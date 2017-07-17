#!/usr/bin/env python
from __future__ import absolute_import
from __future__ import division

#import cProfile

import argparse
import time
import sys
import os
import random

import numpy as np
import tensorflow as tf
from tensorflow.python.client import timeline

from baskets import rnnmodel
from baskets import utils
from baskets import model_helpers
from baskets import hypers
from baskets.rnnmodel import RNNModel
from baskets.batch_helpers import Batcher
from baskets.time_me import time_me


EVAL_PIDS_PER_USER = 2
def evaluate_model(sess, model, batcher):
  total_cost = 0.0
  # The cost measured on just the 'finetuned' metric: the last order
  total_finetune_cost = 0.0
  costvars = [model.cost]
  if not batcher.finetune:
    costvars.append(model.finetune_cost)
  nbatches = 0
  # This seems to work. (Want the pid used for a given user to be the same for each eval run)
  random.seed(1337)
  # TODO: set pids_per_user=-1, and maybe even try smaller final batch
  batches = batcher.get_batches(pids_per_user=EVAL_PIDS_PER_USER, 
      infinite=False,
      allow_smaller_final_batch=False)
  for batch in batches:
    costs = batch_cost(sess, model, batch, 
        train=False, costvars=costvars)
    if batcher.finetune:
      assert len(costs) == 1
      cost = costs[0]
      ft_cost = cost
    else:
      cost, ft_cost = costs
    total_cost += cost
    total_finetune_cost += ft_cost
    nbatches += 1

  # May or may not want to re-seed at this point. 
  #random.seed()
  return total_cost / nbatches, total_finetune_cost / nbatches

# TODO: yeah, this is a dumb leaky abstraction.
def batch_cost(sess, model, batch, train, costvars=None, 
    rmeta=None, roptions=None, lr=None):
  if costvars is None:
    costvars = [model.cost, model.weight_penalty]
  feed = model_helpers.feed_dict_for_batch(batch, model)
  if train:
    feed[model.lr] = lr
  # TODO: this'll break for a model where product_embeddings is False 
  # (though it seems like I'm probably gonna stick with them)
  to_fetch = costvars
  values = sess.run(to_fetch, feed, options=roptions, run_metadata=rmeta)
  return values
    

def train(sess, model, batcher, runlabel, eval_batcher, eval_model, rmeta, roptions, logdir):
  # Setup summary writer.
  summary_writer = tf.summary.FileWriter('{}/{}'.format(logdir, runlabel))
  step = None
  summary_op = model.merged_summary()

  def write_tagged_value(tag, value):
    summ = tf.summary.Summary()
    summ.value.add(tag=tag, simple_value=float(value))
    summary_writer.add_summary(summ, step)
  def write_values(groupname, summary_dict):
    summ = tf.summary.Summary()
    for tag, value in summary_dict.iteritems():
      fulltag = os.path.join(groupname, tag) # sue me
      summ.value.add(tag=fulltag, simple_value=float(value))
    summary_writer.add_summary(summ, step)

  # Calculate trainable params.
  t_vars = tf.trainable_variables()
  count_t_vars = 0
  for var in t_vars:
    num_param = np.prod(var.get_shape().as_list())
    count_t_vars += num_param
    tf.logging.info('%s %s %i', var.name, str(var.get_shape()), num_param)
  tf.logging.info('Total trainable variables %i.', count_t_vars)
  write_tagged_value('Misc/Num_Trainable_Params', count_t_vars)
  summary_writer.flush()
  
  hps = model.hps
  start = time.time()

  log_every = model.hps.log_every
  # Some training stats to accumulate and report every few hundred batches.
  train_costs = np.zeros(log_every)
  train_ft_costs = np.zeros(log_every)
  l2_costs = np.zeros(log_every)

  batch_fetch_time = 0
  batches = batcher.get_batches(infinite=True)
  # TODO: does evaluating the finetune cost have a non-negligible impact 
  # on training speed?
  # TODO: eventually should probably not bother fetching both cost and
  # finetune_cost when --finetune flag is used. But for now it's kind
  # of nice debugging, to make sure they match.
  train_costvars = [model.cost, model.weight_penalty, model.finetune_cost,
      model.train_op]
  # >0 if we're resuming from a checkpoint
  start_step = sess.run(model.global_step)
  for i in range(hps.num_steps):
    step = sess.run(model.global_step)
    tb0 = time.time()
    batch = batches.next()
    tb1 = time.time()
    batch_fetch_time += (tb1 - tb0)
    lr_exponent = i if model.hps.lr_reset else step
    lr = ( (hps.learning_rate - hps.min_learning_rate) *
           (hps.decay_rate)**lr_exponent + hps.min_learning_rate
         )
    vals = batch_cost(sess, model, batch, 
        train=True, rmeta=rmeta, roptions=roptions, 
        costvars=train_costvars, lr=lr)
    bcost, bl2_cost, b_ft_cost, _ = vals
    costi = i % log_every
    train_costs[costi] = bcost
    l2_costs[costi] = bl2_cost
    train_ft_costs[costi] = b_ft_cost
    if (i+1) % log_every == 0:
      # Average cost over last 100 (or whatever) batches
      cost = train_costs.mean()
      l2_cost = l2_costs.mean()
      ft_cost = train_ft_costs.mean()
      end = time.time()
      time_taken = (end - start)

      misc_summ = {'Learning_Rate': lr}
      write_values('Misc', misc_summ)
      time_summ = {'Time_Taken_Train': time_taken/log_every, 
          'Time_Taken_Batchfetch': batch_fetch_time/log_every, }
      write_values('Timing', time_summ)
      loss_summ = {'Basic_Loss': cost, 'Finetune_Loss': ft_cost, 'Weight_Penalty': l2_cost,
          'Total_Cost': cost+l2_cost }
      write_values('Loss/Train', loss_summ)
      
      feed = model_helpers.feed_dict_for_batch(batch, model)
      summ = sess.run(summary_op, feed_dict=feed)
      summary_writer.add_summary(summ, step)

      summary_writer.flush()

      batch_fetch_time = 0

      output_format = ('step: %d/%d, cost: %.4f, train_time_taken: %.3f, lr: %.5f')
      output_values = (step, start_step+hps.num_steps, cost, time_taken, lr)
      output_log = output_format % output_values
      tf.logging.info(output_log)


      start = time.time()
    if (i+1) % hps.save_every == 0 or i == (hps.num_steps - 1):
      utils.save_model(sess, runlabel, step)
    if (i+1) % hps.eval_every == 0:  
      t0 = time.time()
      eval_cost, ft_cost = evaluate_model(sess, eval_model, eval_batcher)
      t1 = time.time()
      eval_time = t1 - t0
      # Cheat the clock on total training time, so it doesn't count time spent 
      # on the validation set
      start += eval_time
      tf.logging.info('Evaluation loss={:.4f} (took {:.1f}s)'.format(eval_cost, eval_time))
      write_tagged_value('Timing/Validation', eval_time)
      validation_summ = {'Loss': eval_cost, 'Finetune_Loss': ft_cost}
      write_values('Loss/Validation', validation_summ)
      summary_writer.flush()


def main():
  tf.logging.set_verbosity(tf.logging.INFO)
  parser = argparse.ArgumentParser()
  parser.add_argument('tag')
  parser.add_argument('--recordfile', default='train.tfrecords', 
      help='tfrecords file with the users to train on (default: train.tfrecords)')
  parser.add_argument('-r', '--resume', metavar='TAG',
      help='Load existing checkpoint with the given tag name and resume training')
  parser.add_argument('--profile', action='store_true')
  parser.add_argument('--finetune', action='store_true')
  parser.add_argument('--logdir', default='logs')
  args = parser.parse_args()

  hps = hypers.hps_for_tag(args.tag, save_full=True)
  tf.logging.info('Building model')
  model = RNNModel(hps)
  tf.logging.info('Loading batcher')
  # TODO: random.seed based on global step
  batcher = Batcher(hps, args.recordfile, 
      in_media_res=args.resume is not None,
      finetune=args.finetune
      )

  eval_hps = hypers.as_eval(hps)
  eval_recordfname = 'eval.tfrecords'
  eval_batcher = Batcher(eval_hps, eval_recordfname, finetune=args.finetune)
  eval_model = RNNModel(eval_hps, reuse=True)

  sess = tf.InteractiveSession()

  run_metadata = None
  run_options = None
  # TODO: I'll probably never try this again because reading the trace is slow af
  if args.profile:
    run_metadata = tf.RunMetadata()
    run_options = tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE)

  if args.resume:
    tf.logging.info('Loading saved weights')
    utils.load_checkpoint_for_tag(args.tag, sess)
  else:
    sess.run(tf.global_variables_initializer())

  with time_me("Completed training"):
    train(sess, model, batcher, args.tag, eval_batcher, eval_model, 
        run_metadata, run_options, args.logdir)

  if args.profile:
    tl = timeline.Timeline(run_metadata.step_stats)
    ctf = tl.generate_chrome_trace_format()
    with open('timeline.json', 'w') as f:
      f.write(ctf)
    print 'Wrote timeline to timeline.json'

if __name__ == '__main__':
  main()
  #cProfile.run('main()', 'runner.profile')