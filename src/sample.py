# """
# Docstring


# This is a rewrite of the iterative pi score subsampling from Roman/Amanda's CTG pacakge.
# Ported to pure python.

# Todo:
#     method documentation
#     unit tests

# """

import re
import os
import argparse
import pandas as pd
import numpy as np

import rpy2.robjects.numpy2ri 
rpy2.robjects.numpy2ri.activate()

from irls import *
from weight import *

from statsmodels.distributions.empirical_distribution import ECDF
import scipy.sparse as sps


def _build_target_array(probes, names):
    """ Description
    """

    # get a list of all probes to targets from names datafame
    a = names[['probe_a_id','target_a_id']]
    b = names[['probe_b_id','target_b_id']]
    a.columns = ['probe_id','target_id']
    b.columns = ['probe_id','target_id']
    c = pd.concat([a,b],axis=0).drop_duplicates()

    # merge together and take target ids
    targets = np.array(pd.merge(pd.DataFrame(probes,columns=['probe_id']), c)['target_id'])
    return targets

def summarize(pi_iter, fitness_iter):

    
    fitness_mean = fitness_iter.mean(axis=1).to_frame()
    pi_mean = pi_iter.mean(axis=1)
    pi_iter_null = pi_iter.subtract(pi_mean, axis=0)

    enull = ECDF( np.concatenate((pi_iter_null.values.flatten(), - pi_iter_null.values.flatten())) )
    emean = ECDF( pi_mean )

    fdr_left = np.minimum(1, enull(pi_mean)/emean(pi_mean))
    fdr_right = np.minimum(1, enull(-pi_mean)/(1-emean(pi_mean)))
    fdr = pd.DataFrame(list(zip(fdr_left, fdr_right)), index=pi_mean.index, columns=['fdr_left','fdr_right'])

    # get per pair sd
    sd = pi_iter.std(axis=1)

    # calculate z score
    pi_mean_sd = np.std(pi_mean)
    z = pi_mean.divide(pi_mean_sd)

    # compute posterior probability
    pp = pi_iter_null.abs().lt(pi_mean.abs(), axis=0).mean(axis=1)

    # concatenate together
    x = pd.concat([pi_mean, fdr, sd, z, pp], axis=1)
    x = x.reset_index()

    # merge in gene fitnesses
    x = pd.merge(x, fitness_mean, left_on='target_id_1', right_index=True, how="left")
    x = pd.merge(x, fitness_mean, left_on='target_id_2', right_index=True, how="left")
    x.columns = ['geneA','geneB','pi_mean', 'fdr_left', 'fdr_right', 'sd', 'z', 'pp', 'fA','fB']

    return x

def subsample(fc, pp, sdfc, seed=None, testing = False):
    """ Subsample the construct fitness matrix according to posterior probabilities
    """
    fc_0 = sps.triu(fc)
    sdfc_0 = sps.triu(sdfc)
    pp_0 = sps.triu(pp)

    # get nonzero positions 
    if testing:
        if seed is None:
            raise(BaseException("If testing for validation must provide a seed."))

        # basically use R random to mimick RS/A iterative pi scores
        # define r functions to get randoms with predefined seed
        rpy2.robjects.r('''
            gen_fc1 <- function(fc_0, sdfc_0, pp_0, iter) {
                utri<-upper.tri(fc_0)
                ntri<-sum(utri)
                nprobes = dim(fc_0)[1]
                fc_1<-matrix(0,nrow=nprobes,ncol=nprobes) 

                set.seed(iter)
                fc0<-fc_0[utri]+rnorm(ntri,sd=sdfc_0[utri]) 
                pp0<-pp_0[utri] 

                set.seed(iter)
                draw<-ifelse(runif(ntri)<pp0,1,0) 
                fc_1[utri]<-fc0*draw 
                fc_1<-fc_1+t(fc_1) 
                fc_1
            }
            ''')
        r_fc = rpy2.robjects.r['gen_fc1']
        fc_0 = np.asarray(fc_0.todense())
        sdfc_0 = np.asarray(sdfc_0.todense())
        pp_0 = np.asarray(pp_0.todense())
        fc_1 = np.array(r_fc(fc_0, sdfc_0, pp_0, seed))
        fc_1 = sps.csr_matrix(fc_1)
    else:  
        # get random noise 
        if seed:
            np.random.seed(seed)

        noise_arr = np.array([np.random.normal(loc=0, scale=sd) if sd>0 else 0 for sd in sdfc_0.data])
        noise = sps.csr_matrix((noise_arr, sdfc_0.nonzero()), shape = sdfc_0.shape)

        # add noise to fc
        fc_0 = fc_0 + noise

        # decide whether to use base on posterior probability 
        # TODO: why randomly draw, should we set a confidence threshold
        if seed:
            np.random.seed(seed)

        #include = pp_0 < np.random.rand(pp_0.shape[0], pp_0.shape[1])
        sampling_arr = np.random.rand(pp_0.count_nonzero())
        include = pp_0 < sps.csr_matrix((sampling_arr, pp_0.nonzero()), shape=pp_0.shape)

        # multiply the construct matrix by boolean inclusion based on posterior proability
        fc_0 = fc_0.multiply(include)

        # make symmeteric
        fc_1 = fc_0 + fc_0.transpose()

    return fc_1

def run_iteration(fc, pp, sdfc, w0, probes, targets,
    ag=2,
    tol=1e-3, 
    maxiter=50,
    n_probes_per_target=2,
    epsilon = 1e-6,
    null_target_id="NonTargetingControl",
    niter=2,
    testing=False,
    use_full_dataset_for_ranking = True,
    all = True
    ):



    """
    Calculate the pi scores by iterative fitting

    Args: 
        fc (matrix): construct fitnesses
        pp (matrix): posterior probabilities 
        sdfc (matrix): the standard deviation of construct fitnesses 
        w0 (matrix): boolean matrix of good constructs
        niter (int): number of iterations to perform


    """

    if use_full_dataset_for_ranking:
        # if we want to use the full dataset for probe rankings, calculate it now
        # get initial fit of probe fitnesses based on construct fitnesses
        fp_0, eij_0 = irls(fc, w0, ag=ag, tol=tol, maxiter=maxiter)

        # if we want to use the full dataset for probe rankings, calculate it now
        # get initial fit of probe fitnesses based on construct fitnesses
        if null_target_id:
            # get the indicies of the null targets
            ix = np.where( targets == null_target_id)
            # get mean of the null probe fitnesses
            null_mean = fp_0[ix].mean()
            # subtract off null mean from all fitnesses
            fp_0 = fp_0 - null_mean

        # rank the probes 
        ranks = rank_probes(fp_0, targets, null_target_id = null_target_id)
    else:
        # if we don't want to use it then simply set the ranking to None
        ranks = None


    pi_iter = None
    fitness_iter = None
    counter = 0
    while counter < niter:
        counter += 1

        logging.info("Performing iteration: {}".format(counter))
        
        if testing:
        	seed = counter
        else:
        	seed = None

        # get subsample 
        fc_1 = subsample(fc, pp, sdfc, seed=seed, testing = testing)

        # get unweighted estimates using subsampled construct fitnesses
        fp, eij = irls(fc_1, w0, ag=ag, tol=tol, maxiter=maxiter, all=all)
           
        # get weighted pi scores and target fitness 
        pi_scores, target_fitness = weight_by_target(eij, fp, w0, probes, targets,
                                                    n_probes_per_target=n_probes_per_target,
                                                    null_target_id = null_target_id,
                                                    pre_computed_ranks = ranks
                                                    )

        # add column labels corresponding to subsample
        pi_scores.columns = [counter]
        target_fitness.columns = [counter]

        # store results
        if pi_iter is None:
            pi_iter = pi_scores.copy()
            fitness_iter = target_fitness.copy()
        else:
            pi_iter = pd.concat([pi_iter, pi_scores], axis=1)
            fitness_iter = pd.concat([fitness_iter, target_fitness], axis=1)

    # return results of subsample
    return pi_iter, fitness_iter 

def run_iteration_multi_condition(fcs, pps, sdfcs, w0s, probes, targets,
    ag=2,
    tol=1e-3, 
    maxiter=50,
    n_probes_per_target=2,
    epsilon = 1e-6,
    null_target_id="NonTargetingControl",
    niter=2,
    testing=False,
    use_full_dataset_for_ranking = True,
    all = True
    ):



    """
    Calculate the pi scores by iterative fitting

    """

    if use_full_dataset_for_ranking:
        # if we want to use the full dataset for probe rankings, calculate it now
        # get initial fit of probe fitnesses based on construct fitnesses
        fp_0,_ = irls_multi_condition(fcs, w0s, ag=ag, tol=tol, maxiter=maxiter)

        # if we want to use the full dataset for probe rankings, calculate it now
        # get initial fit of probe fitnesses based on construct fitnesses
        if null_target_id:
            # get the indicies of the null targets
            ix = np.where( targets == null_target_id)
            # get mean of the null probe fitnesses
            null_mean = fp_0[ix].mean()
            # subtract off null mean from all fitnesses
            fp_0 = fp_0 - null_mean

        # rank the probes 
        ranks = rank_probes(fp_0, targets, null_target_id = null_target_id)
    else:
        # if we don't want to use it then simply set the ranking to None
        ranks = None


    pi_iter = [None for i in range(len(fcs))]
    fitness_iter = [None for i in range(len(fcs))]
    counter = 0
    while counter < niter:
        counter += 1

        logging.info("Performing iteration: {}".format(counter))
        if testing:
            seed = counter
        else:
            seed = None

        fc_1s = []
        for i in range(len(fcs)):
            # get subsample 
            fc_1 = subsample(fcs[i], pps[i], sdfcs[i], seed=seed, testing = testing)
            fc_1s.append(fc_1)


        # get unweighted estimates using subsampled construct fitnesses
        fp, eijs = irls_multi_condition(fc_1s, w0s, ag=ag, tol=tol, maxiter=maxiter, all=all)

        
        for i in range(len(fcs)):

            # get weighted pi scores and target fitness 
            pi_scores, target_fitness = weight_by_target(eijs[i], fp, w0s[i], probes, targets,
                                                        n_probes_per_target=n_probes_per_target,
                                                        null_target_id = null_target_id,
                                                        pre_computed_ranks = ranks
                                                        )

            # add column labels corresponding to subsample
            pi_scores.columns = [counter]
            target_fitness.columns = [counter]

            # store results
            if pi_iter[i] is None:
                pi_iter[i] = pi_scores.copy()
                fitness_iter[i] = target_fitness.copy()
            else:
                pi_iter[i] = pd.concat([pi_iter[i], pi_scores], axis=1)
                fitness_iter[i] = pd.concat([fitness_iter[i], target_fitness], axis=1)


    # return results of the sample
    return pi_iter, fitness_iter 
