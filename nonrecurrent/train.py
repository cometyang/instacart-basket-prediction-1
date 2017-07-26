#!/usr/bin/env python

from __future__ import division
import argparse
import xgboost as xgb
from collections import defaultdict
import logging

from baskets import common
from baskets.time_me import time_me
from baskets.hypers import Mode

from dataset import Dataset
import hypers

# Copied from https://www.kaggle.com/nickycan/lb-0-3805009-python-edition
# TODO: Loas params from config files.
xgb_params = {
    "objective"         : "reg:logistic"
    ,"eval_metric"      : "logloss"
    ,"eta"              : 0.1
    ,"max_depth"        : 6
    ,"min_child_weight" :10
    ,"gamma"            :0.70
    ,"subsample"        :0.76
    ,"colsample_bytree" :0.95
    ,"alpha"            :2e-05
    ,"lambda"           :10
}

P_THRESH = .2
THRESH = (P_THRESH)

counter = 0
def train(traindat, params, tag, hps):
  valdat = Dataset('validation', hps, mode=Mode.eval)
  # TODO: try set_base_margin (https://github.com/dmlc/xgboost/blob/master/demo/guide-python/boost_from_prediction.py)
  dtrain = traindat.as_dmatrix()
  def quick_fscore(preds, _notused_dtrain):
    global counter
    counter += 1
    if 1 and counter % 5 != 0:
      return 'fscore', 0.0
    with time_me('calculated validation fscore', mode='print'):
      user_counts = defaultdict(lambda : dict(tpos=0, fpos=0, fneg=0))
      for i, record in enumerate(valdat.records):
        uid = record['uid']
        pred = preds[i] >= THRESH
        label = record['label']
        if pred and label:
          user_counts[uid]['tpos'] += 1
        elif pred and not label:
          user_counts[uid]['fpos'] += 1
        elif label and not pred:
          user_counts[uid]['fneg'] += 1
      fscore_sum = 0
      for uid, res in user_counts.iteritems():
        numerator = 2 * res['tpos']
        denom = numerator + res['fpos'] + res['fneg']
        if denom == 0:
          fscore = 1
        else:
          fscore = numerator / denom
        fscore_sum += fscore
      return 'fscore', fscore_sum / len(user_counts)
    
  dval = valdat.as_dmatrix()
  # If you pass in more than one value to evals, early stopping uses the
  # last one. Because why not.
  watchlist = [(dtrain, 'train'), (dval, 'validation'),]
  #watchlist = [(dval, 'validation'),]

  model = xgb.train(xgb_params, dtrain, hps.rounds, evals=watchlist, 
      early_stopping_rounds=hps.early_stopping_rounds) #, feval=quick_fscore, maximize=True)
  # Set output_margin=True to get logits
  model_path = common.resolve_xgboostmodel_path(tag)
  model.save_model(model_path)

def main():
  logging.basicConfig(level=logging.INFO)
  parser = argparse.ArgumentParser()
  parser.add_argument('tag')
  parser.add_argument('--train-recordfile', default='train', 
      help='identifier for file with the users to train on (default: train)')
  parser.add_argument('-n', '--n-rounds', type=int, default=50,
      help='Number of rounds of boosting')
  parser.add_argument('--n-users', type=int, 
      help='Limit number of users tested on (default: no limit)')
  parser.add_argument('--weight', action='store_true')
  args = parser.parse_args()

  try:
    hps = hypers.hps_for_tag(args.tag)
  except hypers.NoHpsDefinedException:
    logging.warn('No hps found for tag {}. Creating and saving some.'.format(args.tag))
    hps = hypers.get_default_hparams()
    hps.train_file = args.train_recordfile
    hps.rounds = args.n_rounds
    hps.weight = args.weight
    hypers.save_hps(args.tag, hps)
  with time_me('Loaded train dataset', mode='stderr'):
    dataset = Dataset(hps.train_file, hps, maxlen=args.n_users)
  params = xgb_params
  with time_me(mode='stderr'):
    train(dataset, params, args.tag, hps)

if __name__ == '__main__':
  main()