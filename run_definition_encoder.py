#Refer the LICENSE in this directory
#
#Modified from https://github.com/TimDettmers/ConvE/blob/master/main.py
#Modified by Sawan Kumar: Modified to train encodings of definitions of synsets using ConvE
#

import json
import torch
import pickle
import numpy as np
import argparse
import sys
import os
import math
from tqdm import tqdm

from os.path import join
import torch.backends.cudnn as cudnn

from evaluation import ranking_and_hits
from model import ConvE, DistMult, Complex

from spodernet.preprocessing.pipeline import Pipeline, DatasetStreamer
from spodernet.preprocessing.processors import JsonLoaderProcessors, Tokenizer, AddToVocab, SaveLengthsToState, StreamToHDF5, SaveMaxLengthsToState, CustomTokenizer
from spodernet.preprocessing.processors import ConvertTokenToIdx, ApplyFunction, ToLower, DictKey2ListMapper, ApplyFunction, StreamToBatch
from spodernet.utils.global_config import Config, Backends
from spodernet.utils.logger import Logger, LogLevel
from spodernet.preprocessing.batching import StreamBatcher
from spodernet.preprocessing.pipeline import Pipeline
from spodernet.preprocessing.processors import TargetIdx2MultiTarget
from spodernet.hooks import LossHook, ETAHook
from spodernet.utils.util import Timer
from spodernet.preprocessing.processors import TargetIdx2MultiTarget

from encoder import DefinitionEncoder
from definition_preprocessor import Preprocessor

np.set_printoptions(precision=3)

cudnn.benchmark = True

# 这里有一个处理知识图谱的操作
''' Preprocess knowledge graph using spodernet. '''
def preprocess(dataset_name, delete_data=False):
    full_path = 'data/{0}/e1rel_to_e2_full.json'.format(dataset_name)
    train_path = 'data/{0}/e1rel_to_e2_train.json'.format(dataset_name)
    dev_ranking_path = 'data/{0}/e1rel_to_e2_ranking_dev.json'.format(dataset_name)
    test_ranking_path = 'data/{0}/e1rel_to_e2_ranking_test.json'.format(dataset_name)

    keys2keys = {}
    keys2keys['e1'] = 'e1' # entities
    keys2keys['rel'] = 'rel' # relations
    keys2keys['rel_eval'] = 'rel' # relations
    keys2keys['e2'] = 'e1' # entities
    keys2keys['e2_multi1'] = 'e1' # entity
    keys2keys['e2_multi2'] = 'e1' # entity
    input_keys = ['e1', 'rel', 'rel_eval', 'e2', 'e2_multi1', 'e2_multi2']
    d = DatasetStreamer(input_keys)
    d.add_stream_processor(JsonLoaderProcessors())
    d.add_stream_processor(DictKey2ListMapper(input_keys))

    # process full vocabulary and save it to disk
    d.set_path(full_path)
    p = Pipeline(dataset_name, delete_data, keys=input_keys, skip_transformation=True)
    p.add_sent_processor(ToLower())
    p.add_sent_processor(CustomTokenizer(lambda x: x.split(' ')),keys=['e2_multi1', 'e2_multi2'])
    p.add_token_processor(AddToVocab())
    p.execute(d)
    p.save_vocabs()


    # process train, dev and test sets and save them to hdf5
    p.skip_transformation = False
    for path, name in zip([train_path, dev_ranking_path, test_ranking_path], ['train', 'dev_ranking', 'test_ranking']):
        d.set_path(path)
        p.clear_processors()
        p.add_sent_processor(ToLower())
        p.add_sent_processor(CustomTokenizer(lambda x: x.split(' ')),keys=['e2_multi1', 'e2_multi2'])
        p.add_post_processor(ConvertTokenToIdx(keys2keys=keys2keys), keys=['e1', 'rel', 'rel_eval', 'e2', 'e2_multi1', 'e2_multi2'])
        p.add_post_processor(StreamToHDF5(name, samples_per_file=1000, keys=input_keys))
        p.execute(d)


def main(args, model_path):
    if args.preprocess: preprocess(args.data, delete_data=True)
    input_keys = ['e1', 'rel', 'rel_eval', 'e2', 'e2_multi1', 'e2_multi2']
    p = Pipeline(args.data, keys=input_keys)
    p.load_vocabs()
    vocab = p.state['vocab'] # 都要把数据转换成对象存储起来。这里用的是spodernet 中的Vocab对象

    num_entities = vocab['e1'].num_token # 得到总共有多少个实体（sense） 
    # 生成三批数据
    train_batcher = StreamBatcher(args.data, 'train', args.batch_size, randomize=True, keys=input_keys, loader_threads=args.loader_threads)
    dev_rank_batcher = StreamBatcher(args.data, 'dev_ranking', args.test_batch_size, randomize=False, loader_threads=args.loader_threads, keys=input_keys)
    test_rank_batcher = StreamBatcher(args.data, 'test_ranking', args.test_batch_size, randomize=False, loader_threads=args.loader_threads, keys=input_keys)

    model = ConvE(args, vocab['e1'].num_token, vocab['rel'].num_token)

    train_batcher.at_batch_prepared_observers.insert(1,TargetIdx2MultiTarget(num_entities, 'e2_multi1', 'e2_multi1_binary'))

    # 这部分功能应该是：在训练完之后使用一个回调
    eta = ETAHook('train', print_every_x_batches=args.log_interval)
    train_batcher.subscribe_to_events(eta)
    train_batcher.subscribe_to_start_of_epoch_event(eta)
    train_batcher.subscribe_to_events(LossHook('train', print_every_x_batches=args.log_interval))

    P = Preprocessor("../external/wordnet-mlj12")
    tokenidx_to_synset = vocab['e1'].idx2token

    encoder = DefinitionEncoder()
    encoder.cuda()
    model.cuda()
    if args.initialize:
        model_params = torch.load(args.initialize)
        print(model)
        total_param_size = []
        params = [(key, value.size(), value.numel()) for key, value in model_params.items()]
        for key, size, count in params:
            total_param_size.append(count)
            print(key, size, count)
        print(np.array(total_param_size).sum())
        model.load_state_dict(model_params)
        model.eval()
        ranking_and_hits(model, test_rank_batcher, vocab, 'test_evaluation')
        ranking_and_hits(model, dev_rank_batcher, vocab, 'dev_evaluation')
        # 赋值definition encoder，但是在model的属性中，没有找到 encoder 
        model.encoder = encoder  
        model.encoder.init()
    elif args.resume:
        model.encoder = encoder
        model_params = torch.load(model_path)
        print(model)
        total_param_size = []
        params = [(key, value.size(), value.numel()) for key, value in model_params.items()]
        for key, size, count in params:
            total_param_size.append(count)
            print(key, size, count)
        print(np.array(total_param_size).sum())
        model.load_state_dict(model_params)
        model.eval()
        ranking_and_hits(model, test_rank_batcher, vocab, 'test_evaluation', tokenidx_to_synset, P.get_batch)
        ranking_and_hits(model, dev_rank_batcher, vocab, 'dev_evaluation', tokenidx_to_synset, P.get_batch)
    else:
        model.encoder = encoder
        model.encoder.init()
        model.init()

    total_param_size = []
    params = [value.numel() for value in model.parameters()]
    print(params)
    print(np.sum(params))

    opt = torch.optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.l2)
    best_dev_mrr = 0

    model.eval()
    dev_mrr = ranking_and_hits(model, dev_rank_batcher, vocab, 'dev_evaluation', tokenidx_to_synset, P.get_batch)
    # 准备训练
    for epoch in range(args.epochs):
        model.train()
        for i, str2var in enumerate(train_batcher):
            opt.zero_grad()
            e1 = str2var['e1'] 
            rel = str2var['rel']

            e1_tokens = [tokenidx_to_synset[idx] for idx in e1.detach().cpu().numpy().ravel()]
            batch, lengths = P.get_batch(e1_tokens)

            # e1_emb 就是使用 bilstm 得到的embedding
            e1_emb = model.encoder((batch.cuda(), lengths))[0]

            e2_multi = str2var['e2_multi1_binary'].float()
            # label smoothing
            e2_multi = ((1.0-args.label_smoothing)*e2_multi) + (1.0/e2_multi.size(1))

            # 放到
            pred = model.forward(e1_emb, rel, e1_encoded=True)
            loss = model.loss(pred, e2_multi)
            loss.backward()
            opt.step()

            train_batcher.state.loss = loss.cpu()

        #saving on improvement in dev score
        #print('saving to {0}'.format(model_path))
        #torch.save(model.state_dict(), model_path)

        model.eval()
        with torch.no_grad():
            if epoch % 5 == 0 and epoch > 0:
                dev_mrr = ranking_and_hits(model, dev_rank_batcher, vocab, 'dev_evaluation', tokenidx_to_synset, P.get_batch)
                if dev_mrr > best_dev_mrr:
                    print('saving to {} MRR {}->{}'.format(model_path, best_dev_mrr, dev_mrr))
                    best_dev_mrr = dev_mrr
                    torch.save(model.state_dict(), model_path)

            if epoch % 5 == 0:
                if epoch > 0:
                    ranking_and_hits(model, test_rank_batcher, vocab, 'test_evaluation', tokenidx_to_synset, P.get_batch)

    if args.represent:
        P = Preprocessor()
        synsets = [P.idx_to_synset[idx] for idx in range(len(P.idx_to_synset))]
        embeddings = []
        embeddings_proj = []
        for i in tqdm(range(0, len(synsets), args.test_batch_size)):
            synsets_batch = synsets[i:i+args.test_batch_size]
            with torch.no_grad():
                batch, lengths = P.get_batch(synsets_batch)
                emb_proj, emb = model.encoder((batch.cuda(), lengths))
                embeddings_proj.append(emb_proj.detach().cpu())
                embeddings.append(emb.detach().cpu())
        embeddings = torch.cat(embeddings, 0).numpy()
        embeddings_proj = torch.cat(embeddings_proj, 0).numpy()
        print ('embeddings', embeddings.shape, embeddings_proj.shape)
        basename,ext = os.path.splitext(args.represent)
        fname = args.represent
        np.savez_compressed(fname, embeddings=embeddings, synsets=synsets)
        fname = basename+'_projected'+ext
        np.savez_compressed(fname, embeddings=embeddings_proj, synsets=synsets)


if __name__ == '__main__':
    # 设置参数
    parser = argparse.ArgumentParser(description='Link prediction for knowledge graphs')
    parser.add_argument('--batch-size', type=int, default=128, help='input batch size for training (default: 128)')
    parser.add_argument('--test-batch-size', type=int, default=128, help='input batch size for testing/validation (default: 128)')
    parser.add_argument('--epochs', type=int, default=1000, help='number of epochs to train (default: 1000)')
    parser.add_argument('--lr', type=float, default=0.003, help='learning rate (default: 0.003)')
    parser.add_argument('--seed', type=int, default=17, metavar='S', help='random seed (default: 17)')
    parser.add_argument('--log-interval', type=int, default=100, help='how many batches to wait before logging training status')
    parser.add_argument('--data', type=str, default='FB15k-237', help='Dataset to use: {FB15k-237, YAGO3-10, WN18RR, umls, nations, kinship}, default: FB15k-237')
    parser.add_argument('--l2', type=float, default=0.0, help='Weight decay value to use in the optimizer. Default: 0.0')
    parser.add_argument('--model', type=str, default='conve', help='Choose from: {conve, distmult, complex}')
    parser.add_argument('--model_suffix', type=str, default='', help='Add a custom suffix for saved model name')
    parser.add_argument('--embedding-dim', type=int, default=200, help='The embedding dimension (1D). Default: 200')
    parser.add_argument('--embedding-shape1', type=int, default=20, help='The first dimension of the reshaped 2D embedding. The second dimension is infered. Default: 20')
    parser.add_argument('--hidden-drop', type=float, default=0.3, help='Dropout for the hidden layer. Default: 0.3.')
    parser.add_argument('--input-drop', type=float, default=0.2, help='Dropout for the input embeddings. Default: 0.2.')
    parser.add_argument('--feat-drop', type=float, default=0.2, help='Dropout for the convolutional features. Default: 0.2.')
    parser.add_argument('--lr-decay', type=float, default=0.995, help='Decay the learning rate by this factor every epoch. Default: 0.995')
    parser.add_argument('--loader-threads', type=int, default=4, help='How many loader threads to use for the batch loaders. Default: 4')
    parser.add_argument('--preprocess', action='store_true', help='Preprocess the dataset. Needs to be executed only once. Default: 4')
    parser.add_argument('--resume', action='store_true', help='Resume a model.')
    parser.add_argument('--initialize', type=str, help='conve model to initialize conve params with')
    parser.add_argument('--use-bias', action='store_true', help='Use a bias in the convolutional layer. Default: True')
    parser.add_argument('--label-smoothing', type=float, default=0.1, help='Label smoothing value to use. Default: 0.1')
    parser.add_argument('--hidden-size', type=int, default=9728, help='The side of the hidden layer. The required size changes with the size of the embeddings. Default: 9728 (embedding size 200).')
    parser.add_argument('--represent', type=str, help='specfiy name to get definition representations')
    
    # 将参数再解析出来？
    args = parser.parse_args() # 解析参数

    # parse console parameters and set global variables
    Config.backend = 'pytorch'
    Config.cuda = True
    Config.embedding_dim = args.embedding_dim
    #Logger.GLOBAL_LOG_LEVEL = LogLevel.DEBUG

    # 使用参数来命名模型
    model_name = '{2}_{0}_{1}'.format(args.input_drop, args.hidden_drop, args.model)
    model_path = 'saved_models/{0}_{1}_{2}_defn.model'.format(args.data, model_name, args.model_suffix)

    # 设置随机种子的原因？
    torch.manual_seed(args.seed)
    main(args, model_path)
