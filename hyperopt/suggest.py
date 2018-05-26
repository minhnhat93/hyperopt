from __future__ import print_function
from __future__ import absolute_import
from future import standard_library
from builtins import str
from builtins import object
import six.moves.cPickle as pickle
import functools
import logging
import os
import sys
import time

import numpy as np

from . import pyll
from .utils import coarse_utcnow
from . import base
from . import tpe
from .pyll_utils import scope
from . import hp

from pandas import DataFrame

standard_library.install_aliases()
logger = logging.getLogger(__name__)


class SuggestObject(object):
    """Object for conducting search experiments.
    """
    catch_eval_exceptions = False
    pickle_protocol = -1

    def __init__(self, algo, domain, trials, rstate, async=None,
                 max_queue_len=1,
                 poll_interval_secs=1.0,
                 max_evals=sys.maxsize,
                 verbose=0,
                 ):
        self.algo = algo
        self.domain = domain
        self.trials = trials
        if async is None:
            self.async = trials.async
        else:
            self.async = async
        self.poll_interval_secs = poll_interval_secs
        self.max_queue_len = max_queue_len
        self.max_evals = max_evals
        self.rstate = rstate

        if self.async:
            if 'FMinIter_Domain' in trials.attachments:
                logger.warn('over-writing old domain trials attachment')
            msg = pickle.dumps(
                domain, protocol=self.pickle_protocol)
            # -- sanity check for unpickling
            pickle.loads(msg)
            trials.attachments['FMinIter_Domain'] = msg

    def serial_evaluate(self, N=-1):
        for trial in self.trials._dynamic_trials:
            if trial['state'] == base.JOB_STATE_NEW:
                trial['state'] == base.JOB_STATE_RUNNING
                now = coarse_utcnow()
                trial['book_time'] = now
                trial['refresh_time'] = now
                spec = base.spec_from_misc(trial['misc'])
                ctrl = base.Ctrl(self.trials, current_trial=trial)
                try:
                    result = self.domain.evaluate(spec, ctrl)
                except Exception as e:
                    logger.info('job exception: %s' % str(e))
                    trial['state'] = base.JOB_STATE_ERROR
                    trial['misc']['error'] = (str(type(e)), str(e))
                    trial['refresh_time'] = coarse_utcnow()
                    if not self.catch_eval_exceptions:
                        # -- JOB_STATE_ERROR means this trial
                        #    will be removed from self.trials.trials
                        #    by this refresh call.
                        self.trials.refresh()
                        raise
                else:
                    trial['state'] = base.JOB_STATE_DONE
                    trial['result'] = result
                    trial['refresh_time'] = coarse_utcnow()
                N -= 1
                if N == 0:
                    break
        self.trials.refresh()

    def block_until_done(self):
        already_printed = False
        if self.async:
            unfinished_states = [base.JOB_STATE_NEW, base.JOB_STATE_RUNNING]

            def get_queue_len():
                return self.trials.count_by_state_unsynced(unfinished_states)

            qlen = get_queue_len()
            while qlen > 0:
                if not already_printed:
                    logger.info('Waiting for %d jobs to finish ...' % qlen)
                    already_printed = True
                time.sleep(self.poll_interval_secs)
                qlen = get_queue_len()
            self.trials.refresh()
        else:
            self.serial_evaluate()

    def run(self, N, block_until_done=True):
        """
        block_until_done  means that the process blocks until ALL jobs in
        trials are not in running or new state

        """
        trials = self.trials
        algo = self.algo
        n_queued = 0

        def get_queue_len():
            return self.trials.count_by_state_unsynced(base.JOB_STATE_NEW)

        stopped = False
        while n_queued < N:
            qlen = get_queue_len()
            while qlen < self.max_queue_len and n_queued < N:
                n_to_enqueue = min(self.max_queue_len - qlen, N - n_queued)
                new_ids = trials.new_trial_ids(n_to_enqueue)
                self.trials.refresh()
                if 0:
                    for d in self.trials.trials:
                        print('trial %i %s %s' % (d['tid'], d['state'],
                                                  d['result'].get('status')))
                new_trials = algo(new_ids, self.domain, trials,
                                  self.rstate.randint(2 ** 31 - 1))
                assert len(new_ids) >= len(new_trials)
                if len(new_trials):
                    self.trials.insert_trial_docs(new_trials)
                    self.trials.refresh()
                    n_queued += len(new_trials)
                    qlen = get_queue_len()
                else:
                    stopped = True
                    break

            if self.async:
                # -- wait for workers to fill in the trials
                time.sleep(self.poll_interval_secs)
            else:
                # -- loop over trials and do the jobs directly
                self.serial_evaluate()

            if stopped:
                break

        if block_until_done:
            self.block_until_done()
            self.trials.refresh()
            logger.info('Queue empty, exiting run.')
        else:
            qlen = get_queue_len()
            if qlen:
                msg = 'Exiting run, not waiting for %d jobs.' % qlen
                logger.info(msg)

    def __iter__(self):
        return self

    def __next__(self):
        self.run(1, block_until_done=self.async)
        if len(self.trials) >= self.max_evals:
            raise StopIteration()
        return self.trials


def suggest(fn, space, trials=None, num_trials=100):
    '''
    :param fn: objective function. if multivariate must be decorated with @scope.define.
    :param space: space for each hyperparameter to optimize. example:
    univariate: hp.uniform('x', -10, 10)
    multivariate: scope(hp.uniform('x', -10, 10), hp.uniform(y, 0, 10))
    :param trials: Trials object that contains previously computed data
    :param num_trials: number of trials to run
    :return: a trial object that contain run information of all the trials. get the best trial with trials.argmin
    '''
    algo = tpe.suggest
    if trials is None:
        trials = base.Trials()

    env_rseed = os.environ.get('HYPEROPT_FMIN_SEED', '')
    if env_rseed:
        rstate = np.random.RandomState(int(env_rseed))
    else:
        rstate = np.random.RandomState()

    domain = base.Domain(fn, space, pass_expr_memo_ctrl=None)

    rval = SuggestObject(algo, domain, trials, max_evals=sys.maxsize,
                         rstate=rstate,
                         verbose=0)
    rval.catch_eval_exceptions = True
    rval.run(num_trials, rval.async)
    return trials
# -- flake8 doesn't like blank last line
