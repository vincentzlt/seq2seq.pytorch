#!/usr/bin/env python
# -*- coding: utf-8 -*-
import argparse
import logging
import os
from ast import literal_eval
from datetime import datetime

import torch.backends.cudnn as cudnn
import torch.nn as nn
import torch.nn.parallel
import torch.optim

import seq2seq.tools.trainer as trainers
from seq2seq import datasets, models
from seq2seq.tools.config import PAD
from seq2seq.tools.utils.log import setup_logging

parser = argparse.ArgumentParser(description='PyTorch Seq2Seq Training')
parser.add_argument('--dataset', metavar='DATASET', default='WMT16_de_en',
                    choices=datasets.__all__,
                    help='dataset used: ' +
                    ' | '.join(datasets.__all__) +
                    ' (default: WMT16_de_en)')
parser.add_argument('--dataset_dir', metavar='DATASET_DIR',
                    help='dataset dir')
parser.add_argument('--data_config',
                    default="{'tokenization':'bpe', 'num_symbols':32000, 'shared_vocab':True}",
                    help='data configuration')
parser.add_argument('--results_dir', metavar='RESULTS_DIR', default='./results',
                    help='results dir')
parser.add_argument('--save', metavar='SAVE', default='',
                    help='saved folder')
parser.add_argument('--model', metavar='MODEL', default='RecurrentAttentionSeq2Seq',
                    choices=models.__all__,
                    help='model architecture: ' +
                    ' | '.join(models.__all__) +
                    ' (default: RecurrentAttentionSeq2Seq)')
parser.add_argument('--model_config', default="{'hidden_size:256','num_layers':2}",
                    help='architecture configuration')
parser.add_argument('--devices', default='0',
                    help='device assignment (e.g "0,1", {"encoder":0, "decoder":1})')
parser.add_argument('--trainer', metavar='TRAINER', default='Seq2SeqTrainer',
                    choices=trainers.__all__,
                    help='trainer used: ' +
                    ' | '.join(trainers.__all__) +
                    ' (default: Seq2SeqTrainer)')
parser.add_argument('--type', default='torch.FloatTensor',
                    help='type of tensor - e.g torch.cuda.HalfTensor')
parser.add_argument('-j', '--workers', default=8, type=int,
                    help='number of data loading workers (default: 8)')
parser.add_argument('--epochs', default=90, type=int,
                    help='number of total epochs to run')
parser.add_argument('--start-epoch', default=0, type=int,
                    help='manual epoch number (useful on restarts)')
parser.add_argument('-b', '--batch-size', default=32, type=int,
                    help='mini-batch size (default: 32)')
parser.add_argument('--pack_encoder_inputs', action='store_true',
                    help='pack encoder inputs for rnns')
parser.add_argument('--optimization_config',
                    default="{0: {'optimizer': SGD, 'lr':0.1, 'momentum':0.9}}",
                    type=str, metavar='OPT',
                    help='optimization regime used')
parser.add_argument('--print-freq', default=50, type=int,
                    help='print frequency (default: 10)')
parser.add_argument('--save-freq', default=1000, type=int,
                    help='save frequency (default: 10)')
parser.add_argument('--eval-freq', default=2500, type=int,
                    help='evaluation frequency (default: 10)')
parser.add_argument('--resume', default='', type=str, metavar='PATH',
                    help='path to latest checkpoint (default: none)')
parser.add_argument('-e', '--evaluate', type=str, metavar='FILE',
                    help='evaluate model FILE on validation set')
parser.add_argument('--grad_clip', default=5., type=float,
                    help='maximum grad norm value')
parser.add_argument('--embedding_grad_clip', default=None, type=float,
                    help='maximum embedding grad norm value')
parser.add_argument('--uniform_init', default=None, type=float,
                    help='if value not None - init weights to U(-value,value)')
parser.add_argument('--max_length', default=100, type=int,
                    help='maximum sequence length')


def main(args):
    # set up save path
    time_stamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    if args.evaluate:
        args.results_dir = '/tmp'
    if args.save is '':
        args.save = time_stamp
    save_path = os.path.join(args.results_dir, args.save)
    if not os.path.exists(save_path):
        os.makedirs(save_path)

    # set up logging
    setup_logging(os.path.join(save_path, 'log_%s.txt' % time_stamp))

    logging.info("saving to %s", save_path)
    logging.debug("run arguments: %s", args)

    # set up cuda
    args.devices = literal_eval(args.devices)
    if 'cuda' in args.type:
        main_gpu = 0
        if isinstance(args.devices, tuple):
            main_gpu = args.devices[0]
        elif isinstance(args.devices, int):
            main_gpu = args.devices
        elif isinstance(args.devices, dict):
            main_gpu = args.devices.get('input', 0)
        torch.cuda.set_device(main_gpu)
        cudnn.benchmark = True
    # set dataset
    dataset = getattr(datasets, args.dataset)
    args.data_config = literal_eval(args.data_config)
    train_data = dataset(args.dataset_dir, split='train', **args.data_config)
    val_data = dataset(args.dataset_dir, split='dev', **args.data_config)
    src_tok, target_tok = train_data.tokenizers.values()

    regime = literal_eval(args.optimization_config)
    model_config = literal_eval(args.model_config)

    model_config.setdefault('encoder', {})
    model_config.setdefault('decoder', {})
    if hasattr(src_tok, 'vocab_size'):
        model_config['encoder']['vocab_size'] = src_tok.vocab_size
    model_config['decoder']['vocab_size'] = target_tok.vocab_size
    model_config['vocab_size'] = model_config['decoder']['vocab_size']
    args.model_config = model_config

    model = getattr(models, args.model)(**model_config)
    batch_first = getattr(model, 'batch_first', False)

    logging.info(model)

    # define data loaders
    train_loader = train_data.get_loader(batch_size=args.batch_size,
                                         batch_first=batch_first,
                                         shuffle=True,
                                         pack=args.pack_encoder_inputs,
                                         max_length=args.max_length,
                                         num_workers=args.workers)
    val_loader = val_data.get_loader(batch_size=args.batch_size,
                                     batch_first=batch_first,
                                     shuffle=False,
                                     pack=args.pack_encoder_inputs,
                                     max_length=args.max_length,
                                     num_workers=args.workers)

    trainer_options = dict(
        grad_clip=args.grad_clip,
        embedding_grad_clip=args.embedding_grad_clip,
        save_path=save_path,
        save_info={'tokenizers': train_data.tokenizers,
                   'config': args},
        regime=regime,
        devices=args.devices,
        print_freq=args.print_freq,
        save_freq=args.save_freq,
        eval_freq=args.eval_freq)

    trainer_options['model'] = model
    trainer = getattr(trainers, args.trainer)(**trainer_options)
    num_parameters = sum([l.nelement() for l in model.parameters()])
    logging.info("number of parameters: %d", num_parameters)

    model.type(args.type)
    if args.uniform_init is not None:
        for param in model.parameters():
            param.data.uniform_(args.uniform_init, -args.uniform_init)

    # optionally resume from a checkpoint
    if args.evaluate:
        trainer.load(args.evaluate)
    elif args.resume:
        checkpoint_file = args.resume
        if os.path.isdir(checkpoint_file):
            results.load(os.path.join(checkpoint_file, 'results.csv'))
            checkpoint_file = os.path.join(
                checkpoint_file, 'model_best.pth.tar')
        if os.path.isfile(checkpoint_file):
            trainer.load(checkpoint_file)
        else:
            logging.error("no checkpoint found at '%s'", args.resume)

    logging.info('training regime: %s', regime)
    trainer.epoch = args.start_epoch

    while trainer.epoch < args.epochs:
        # train for one epoch
        trainer.run(train_loader, val_loader)


if __name__ == '__main__':
    args = parser.parse_args()
    main(args)
