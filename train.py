#!/usr/bin/env python

import numpy as np
import matplotlib
from numpy import dtype
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import glob, re, sklearn, collections, argparse, sys, os
import sklearn.metrics
import caffe
from caffe.proto.caffe_pb2 import NetParameter, SolverParameter
import google.protobuf.text_format as prototxt
import time

'''Script for training a neural net model from gnina grid data.
A model template is provided along with training and test sets of the form
<prefix>[train|test][num].types
Test accuracy, as measured by AUC, is periodically assessed.
At the end graphs are made.'''


def write_model_file(model_file, template_file, train_file, test_file, root_folder, avg_rotations=False,
                     train_file2=None, ratio=None, root_folder2=None, test_root_folder=None):
    '''Writes a model prototxt file based on a provided template file
    with certain placeholders replaced in each MolGridDataLayer.
    For the source parameter, "TRAINFILE" is replaced with train_file
    and "TESTFILE" is replaced with test_file.
    For the root_folder parameter, "DATA_ROOT" is replaced with root_folder,
    unless the layer is TEST phase and test_root_folder is provided,
    then it is replaced with test_root_folder.
    For the source2 parameter, "TRAINFILE2" is replaced with train_file2,
    and in the same layer the source_ratio parameter is set to ratio.
    For the root_folder2 parameter, "DATA_ROOT2" is replaced with root_folder2.
    If the avg_rotations argument is set and the layer is TEST phase,
    the rotate parameter is set to 24.'''
    param = NetParameter()
    with open(template_file, 'r') as f:
        prototxt.Merge(f.read(), param)
    for layer in param.layer:
        if layer.molgrid_data_param.source == 'TRAINFILE':
            layer.molgrid_data_param.source = train_file
        if layer.molgrid_data_param.source == 'TESTFILE':
            layer.molgrid_data_param.source = test_file
        if layer.molgrid_data_param.root_folder == 'DATA_ROOT':
            if test_root_folder and 'TEST' in str(layer):
                layer.molgrid_data_param.root_folder = test_root_folder
            else:
                layer.molgrid_data_param.root_folder = root_folder
        if train_file2 and layer.molgrid_data_param.source2 == 'TRAINFILE2':
            layer.molgrid_data_param.source2 = train_file2
            layer.molgrid_data_param.source_ratio = ratio
        if root_folder2 and layer.molgrid_data_param.root_folder2 == 'DATA_ROOT2':
            layer.molgrid_data_param.root_folder2 = root_folder2
        if avg_rotations and 'TEST' in str(layer):
            layer.molgrid_data_param.rotate = 24 #TODO axial rotations aren't working
            #layer.molgrid_data_param.random_rotation = True
    with open(model_file, 'w') as f:
        f.write(str(param))


def write_solver_file(solver_file, train_model, test_models, type, base_lr, momentum, weight_decay,
                      lr_policy, gamma, power, random_seed, max_iter, snapshot_prefix):
    '''Writes a solver prototxt file with parameters set to the
    corresponding argument values. In particular, the train_net
    parameter is set to train_model, and a test_net parameter is
    added for each of test_models, which should be a list.'''
    param = SolverParameter()
    param.train_net = train_model
    for test_model in test_models:
        param.test_net.append(test_model)
        param.test_iter.append(0) #don't test automatically
    param.test_interval = max_iter
    param.type = type
    param.base_lr = base_lr
    param.momentum = momentum
    param.weight_decay = weight_decay
    param.lr_policy = lr_policy
    param.gamma = gamma
    param.power = power
    param.display = 0 #don't print solver iterations
    param.random_seed = random_seed
    param.max_iter = max_iter
    param.snapshot_prefix = snapshot_prefix
    with open(solver_file,'w') as f:
        f.write(str(param))


def evaluate_test_net(test_net, n_tests, rotations):
    '''Evaluate a test network and return the results. The number of
    examples in the file the test_net reads from must equal n_tests,
    otherwise output will be misaligned. Can optionally take the average
    of multiple rotations of each example. Batch size should be 1 and
    other parameters should be set so that data access is sequential.'''
    print("evaulating netowrk")
    #evaluate each example with each rotation
    y_true = []
    y_scores = [[] for _ in xrange(n_tests)]
    y_affinity = []
    y_predaffs = [[] for _ in xrange(n_tests)]
    losses = []
    print("evaluate")
    for r in xrange(rotations):
        for x in xrange(n_tests): #TODO handle different batch sizes
            res = test_net.forward()
            if r == 0:
                y_true.append(float(res['labelout']))
            else:
                assert res['labelout'] == y_true[x] #sanity check
            
            if 'output' in res:
                y_scores[x].append(float(res['output'][0][1])) 
            if 'affout' in res:
                if r == 0:
                    y_affinity.append(float(res['affout']))
                else:
                    assert res['affout'] == y_affinity[x] #sanity check
                y_predaffs[x].append(float(res['predaff']))
            if 'loss' in res:
                losses.append(float(res['loss']))

    #average the scores from each rotation
    y_score = []
    y_predaff = []
    #for x in xrange(n_tests):
        #y_score.append(np.mean(y_scores[x]))

    if y_affinity:
        for x in range(n_tests):
            y_predaff.append(np.mean(y_predaffs[x]))
	    
    #compute auc
    #assert len(np.unique(y_true)) > 1
    auc = 0
    print("the AUC is",auc)
    #compute mean squared error (rmsd) of affinity (for actives only)
    if y_affinity:
        y_predaff = np.array(y_predaff)
        y_affinity = np.array(y_affinity)
        yt = np.array(y_true, np.bool)
        rmsd = sklearn.metrics.mean_squared_error(y_affinity[yt], y_predaff[yt])
    else:
        rmsd = None
        print("none")
    #compute mean loss
    if losses:
        loss = np.mean(losses) 
	print("the losses are", loss)
    else:
        loss = 0
	
    print(y_affinity,y_predaff)	
    return auc, y_true, loss, rmsd, y_affinity, y_predaff


def count_lines(file):
    return sum(1 for line in open(file, 'r'))


def train_and_test_model(args, files, outname):
    '''Train caffe model for iterations steps using provided model template
    and training file(s), and every test_interval iterations evaluate each
    of the train and test files. Return AUC (and RMSD, if affinity model)
    for every test iteration, and also the labels and predictions for the
    final test iteration.'''
    print("about to train")
    template = args.model
    test_interval = args.test_interval
    iterations = args.iterations
    print("pow")
    if test_interval > iterations: #need to test once
        test_interval = iterations

    if args.avg_rotations:
        rotations = 24
    else:
        rotations = 1

    pid = os.getpid()

    #write model prototxts (for each file to test)
    test_model = 'traintest.%d.prototxt' % pid
    train_model = 'traintrain.%d.prototxt' % pid
    test_models = [test_model, train_model]
    test_files = [files['test'], files['train']]
    test_roots = [args.data_root, args.data_root] #which data_root to use
    print("alpha")
    if args.reduced:
        reduced_test_model = 'trainreducedtest.%d.prototxt' % pid
        reduced_train_model = 'trainreducedtrain.%d.prototxt' % pid
        test_models += [reduced_test_model, reduced_train_model]
        test_files += [files['reduced_test'], files['reduced_train']]
        test_roots += [args.data_root, args.data_root]
    if args.prefix2:
        test2_model = 'traintest2.%d.prototxt' % pid
        train2_model = 'traintrain2.%d.prototxt' % pid
        test_models += [test2_model, train2_model]
        test_files += [files['test2'], files['train2']]
        test_roots += [args.data_root2, args.data_root2]
        if args.reduced:
            reduced_test2_model = 'trainreducedtest2.%d.prototxt' % pid
            reduced_train2_model = 'trainreducedtrain2.%d.prototxt' % pid
            test_models += [reduced_test2_model, reduced_train2_model]
            test_files += [files['reduced_test2'], files['reduced_train2']]
            test_roots += [args.data_root2, args.data_root2]

    for test_model, test_file, test_root in zip(test_models, test_files, test_roots):
        if args.prefix2:
            write_model_file(test_model, template, files['train'], test_file, args.data_root, args.avg_rotations,
                             files['train2'], args.data_ratio, args.data_root2, test_root)
        else:
            write_model_file(test_model, template, files['train'], test_file, args.data_root, args.avg_rotations)

    #write solver prototxt
    solverf = 'solver.%d.prototxt' % pid
    write_solver_file(solverf, test_models[0], test_models, args.solver, args.base_lr, args.momentum, args.weight_decay,
                      args.lr_policy, args.gamma, args.power, args.seed, iterations+args.cont, outname)
        
    #set up solver in caffe
    if args.gpu >= 0:
        caffe.set_device(args.gpu)
    caffe.set_mode_gpu()
    solver = caffe.get_solver(solverf)
    if args.cont:
        solver.restore(solvername)
        solver.testall() #link testnets to train net
    if args.weights:
        solver.net.copy_from(args.weights)

    test_nets = {}
    for key, test_file in files.items():
        idx = test_files.index(test_file)
        test_nets[key] = solver.test_nets[idx], count_lines(test_file)
	
    print(test_nets)
    if args.cont:
        mode = 'a'    
        modelname = '%s_iter_%d.caffemodel' % (outname, args.cont)
        solvername = '%s_iter_%d.solverstate' % (outname, args.cont)
    else:
        mode = 'w'
    outfile = '%s.out' % outname
    out = open(outfile, mode, 0) #unbuffered

    #return evaluation results:
    #  auc, loss, and rmsd from each test
    #  y_true, y_score, y_aff, y_predaff from last test
    res = {}
    test_vals = {'auc':[], 'y_true':[], 'y_score':[], 'loss':[], 'rmsd':[], 'y_aff':[], 'y_predaff':[]}
    train_vals = {'auc':[], 'y_true':[], 'y_score':[], 'loss':[], 'rmsd':[], 'y_aff':[], 'y_predaff':[]}
    if args.prefix2:
        test2_vals = {'auc':[], 'y_true':[], 'y_score':[], 'loss':[], 'rmsd':[], 'y_aff':[], 'y_predaff':[]}
        train2_vals = {'auc':[], 'y_true':[], 'y_score':[], 'loss':[], 'rmsd':[], 'y_aff':[], 'y_predaff':[]}

    #also keep track of best test and train aucs
    best_test_auc = 0
    best_train_auc = 0
    best_train_interval = 0

    i_time_avg = 0
    print("power")
    for i in xrange(iterations/test_interval):
        last_test = i == iterations/test_interval-1

        #train
        i_start = start = time.time()
        solver.step(test_interval)
        print "Iteration %d" % (args.cont + (i+1)*test_interval)
        print "Train time: %f" % (time.time()-start)
	
	print("candy")
        #evaluate test set
        start = time.time()
        if args.reduced and not last_test:
            test_net, n_tests = test_nets['reduced_test']
        else:
            test_net, n_tests = test_nets['test']
        test_auc, y_true, _, test_rmsd, y_aff, y_predaff = evaluate_test_net(test_net, n_tests, rotations)
        print "Eval test time: %f" % (time.time()-start)
	print("We tested")
        if i > 0 and not (args.reduced and last_test): #check alignment
            assert np.all(y_true == test_vals['y_true'])
            assert np.all(y_aff == test_vals['y_aff'])

        test_vals['y_true'] = y_true
        test_vals['y_aff'] = y_aff
        #test_vals['y_score'] = y_score
        test_vals['y_predaff'] = y_predaff
        print "Test AUC: %f" % test_auc
        test_vals['auc'].append(test_auc)
        if test_rmsd:
            print "Test RMSD: %f" % test_rmsd
            test_vals['rmsd'].append(test_rmsd)

        if test_auc > best_test_auc:
            best_test_auc = test_auc
            if args.keep_best:
                solver.snapshot() #a bit too much - gigabytes of data
	
	print("dance")
        if args.prefix2:
            #evaluate test set 2
            start = time.time()
            if args.reduced and not last_test:
                test_net, n_tests = test_nets['reduced_test2']
            else:
                test_net, n_tests = test_nets['test2']
            test2_auc, y_true, _, test2_rmsd, y_aff, y_predaff = evaluate_test_net(test_net, n_tests, rotations)
            print "Eval test2 time: %f" % (time.time()-start)

            if i > 0 and not (args.reduced and last_test): #check alignment
                assert np.all(y_true == test2_vals['y_true'])
                assert np.all(y_aff == test2_vals['y_aff'])
	    print("We tested(2)")	
            test2_vals['y_true'] = y_true
            test2_vals['y_aff'] = y_aff
            #test2_vals['y_score'] = y_score
            test2_vals['y_predaff'] = y_predaff
            print "Test2 AUC: %f" % test2_auc
            test2_vals['auc'].append(test2_auc)
            if test2_rmsd:
                print "Test2 RMSD: %f" % test2_rmsd
                test2_vals['rmsd'].append(test2_rmsd)

        #evaluate train set
        start = time.time()
        if args.reduced and not last_test:
            test_net, n_tests = test_nets['reduced_train']
        else:
            test_net, n_tests = test_nets['train']
        train_auc, y_true, train_loss, train_rmsd, y_aff, y_predaff = evaluate_test_net(test_net, n_tests, rotations)
        print "Eval train time: %f" % (time.time()-start)

        if i > 0 and not (args.reduced and last_test): #check alignment
            assert np.all(y_true == train_vals['y_true'])
            assert np.all(y_aff == train_vals['y_aff'])

        train_vals['y_true'] = y_true
        train_vals['y_aff'] = y_aff
        #train_vals['y_score'] = y_score
        train_vals['y_predaff'] = y_predaff
        print "Train AUC: %f" % train_auc
        train_vals['auc'].append(train_auc)
        print "Train loss: %f" % train_loss
        train_vals['loss'].append(train_loss)
        if train_rmsd:
            print "Train RMSD: %f" % train_rmsd
            train_vals['rmsd'].append(train_rmsd)

        if train_auc > best_train_auc:
            best_train_auc = train_auc
            best_train_interval = i

        #check for improvement
        if args.dynamic:
            lr = solver.get_base_lr()
            if (i-best_train_interval) > args.step_when: #reduce learning rate
                lr *= args.step_reduce
                solver.set_base_lr(lr)
                best_train_interval = i #reset 
                best_train_auc = train_auc #the value too, so we can consider the recovery
            if lr < args.step_end:
                break #end early  

        if args.prefix2:
            #evaluate train set
            start = time.time()
            if args.reduced and not last_test:
                test_net, n_tests = test_nets['reduced_train2']
            else:
                test_net, n_tests = test_nets['train2']
            train2_auc, y_true, train2_loss, train2_rmsd, y_aff, y_predaff = evaluate_test_net(test_net, n_tests, rotations)
            print "Eval train2 time: %f" % (time.time()-start)

            if i > 0 and not (args.reduced and last_test): #check alignment
                assert np.all(y_true == train2_vals['y_true'])
                assert np.all(y_aff == train2_vals['y_aff'])

            train2_vals['y_true'] = y_true
            train2_vals['y_aff'] = y_aff
            #train2_vals['y_score'] = y_score
            train2_vals['y_predaff'] = y_predaff
            print "Train2 AUC: %f" % train2_auc
            train2_vals['auc'].append(train2_auc)
            print "Train2 loss: %f" % train2_loss
            train2_vals['loss'].append(train2_loss)
            if train2_rmsd:
                print "Train2 RMSD: %f" % train2_rmsd
                train2_vals['rmsd'].append(train2_rmsd)

        #write out evaluation results
        out.write('%.4f %.4f %.6f %.6f' % (test_auc, train_auc, train_loss, solver.get_base_lr()))
        if None not in (test_rmsd, train_rmsd):
            out.write(' %.4f %.4f' % (test_rmsd, train_rmsd))
        if args.prefix2:
            out.write(' %.4f %.4f %.6f' % (test2_auc, train2_auc, train2_loss))
            if None not in (test2_rmsd, train2_rmsd):
                out.write(' %.4f %.4f' % (test2_rmsd, train2_rmsd))
        out.write('\n')
        out.flush()

        #track avg time per loop
        i_time = time.time()-i_start
        i_time_avg = (i*i_time_avg + i_time)/(i+1)
        i_left = iterations/test_interval - (i+1)
        time_left = i_time_avg * i_left
        print "Loop time: %f (%.2fh left)" % (i_time, time_left/3600.)

    out.close()
    solver.snapshot()
    del solver #free mem
    
    if not args.keep:
        os.remove(solverf)
        for test_model in test_models:
            os.remove(test_model)

    if args.prefix2:
        return test_vals, train_vals, test2_vals, train2_vals
    else:
        return test_vals, train_vals


def write_finaltest_file(finaltest_file, y_true, y_score, footer, mode):

    with open(finaltest_file, mode) as out:
        for (label, score) in zip(y_true, y_score):
            out.write('%f %f\n' % (label, score))
        out.write(footer)


def last_iters_statistics(test_aucs, iterations, test_interval, last_iters):

    last_iters_test_aucs = []
    last_iters = 1000
    if last_iters > iterations:
        last_iters = iterations
    num_test_aucs = last_iters/test_interval
    		
    for fold_test_aucs in test_aucs:
        a = fold_test_aucs[-num_test_aucs:]
        if a:
            last_iters_test_aucs.append(a)
    return np.mean(last_iters_test_aucs), np.max(last_iters_test_aucs), np.min(last_iters_test_aucs)


def training_plot(plot_file, train_series, test_series):

    fig = plt.figure()
    plt.plot(train_series, label='Train')
    plt.plot(test_series, label='Test')
    plt.legend(loc='best')
    plt.savefig(plot_file, bbox_inches='tight')


def plot_roc_curve(plot_file, fpr, tpr, auc, txt):

    fig = plt.figure(figsize=(8,8))
    plt.plot(fpr, tpr, label='CNN (AUC=%.2f)' % auc, linewidth=4)
    plt.legend(loc='lower right',fontsize=20)
    plt.xlabel('False Positive Rate',fontsize=22)
    plt.ylabel('True Positive Rate',fontsize=22)
    plt.axes().set_aspect('equal')
    plt.tick_params(axis='both', which='major', labelsize=16)
    plt.text(.05, -.25, txt, fontsize=22)
    plt.savefig(plot_file, bbox_inches='tight')


def plot_correlation(plot_file, y_aff, y_predaff, rmsd, r2):

    fig = plt.figure(figsize=(8,8))
    plt.plot(y_aff, y_predaff, 'o', label='RMSD=%.2f, R^2=%.3f (Pos)' % (rmsd, r2))
    plt.legend(loc='best', fontsize=20, numpoints=1)
    lo = np.min([np.min(y_aff), np.min(y_predaff)])
    hi = np.max([np.max(y_aff), np.max(y_predaff)])
    plt.xlim(lo, hi)
    plt.ylim(lo, hi)
    plt.xlabel('Experimental Affinity', fontsize=22)
    plt.ylabel('Predicted Affinity', fontsize=22)
    plt.axes().set_aspect('equal')
    plt.savefig(plot_file, bbox_inches='tight')        


def comma_separated_ints(ints):
     return [int(i) for i in ints.split(',') if i and i != 'None']


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='Train neural net on .types data.')
    parser.add_argument('-m','--model',type=str,required=True,help="Model template. Must use TRAINFILE and TESTFILE")
    parser.add_argument('-p','--prefix',type=str,required=True,help="Prefix for training/test files: <prefix>[train|test][num].types")
    parser.add_argument('-d','--data_root',type=str,required=False,help="Root folder for relative paths in train/test files",default='')
    parser.add_argument('-n','--foldnums',type=comma_separated_ints,required=False,help="Fold numbers to run, default is '0,1,2'",default='0,1,2')
    parser.add_argument('-a','--allfolds',action='store_true',required=False,help="Train and test file with all data folds, <prefix>.types",default=False)
    parser.add_argument('-i','--iterations',type=int,required=False,help="Number of iterations to run,default 10,000",default=10000)
    parser.add_argument('-s','--seed',type=int,help="Random seed, default 42",default=42)
    parser.add_argument('-t','--test_interval',type=int,help="How frequently to test (iterations), default 40",default=40)
    parser.add_argument('-o','--outprefix',type=str,help="Prefix for output files, default <model>.<pid>",default='')
    parser.add_argument('-g','--gpu',type=int,help='Specify GPU to run on',default=-1)
    parser.add_argument('-c','--cont',type=int,help='Continue a previous simulation from the provided iteration (snapshot must exist)',default=0)
    parser.add_argument('-k','--keep',action='store_true',default=False,help="Don't delete prototxt files")
    parser.add_argument('-r', '--reduced', action='store_true',default=False,help="Use a reduced file for model evaluation if exists(<prefix>[_reducedtrain|_reducedtest][num].types)")
    parser.add_argument('--avg_rotations', action='store_true',default=False, help="Use the average of the testfile's 24 rotations in its evaluation results")
    #parser.add_argument('-v,--verbose',action='store_true',default=False,help='Verbose output')
    parser.add_argument('--keep_best',action='store_true',default=False,help='Store snapshots everytime test AUC improves')
    parser.add_argument('--dynamic',action='store_true',default=False,help='Attempt to adjust the base_lr in response to training progress')
    parser.add_argument('--solver',type=str,help="Solver type. Default is SGD",default='SGD')
    parser.add_argument('--lr_policy',type=str,help="Learning policy to use. Default is inv.",default='inv')
    parser.add_argument('--step_reduce',type=float,help="Reduce the learning rate by this factor with dynamic stepping, default 0.5",default='0.5')
    parser.add_argument('--step_end',type=float,help='Terminate training if learning rate gets below this amount',default=0)
    parser.add_argument('--step_when',type=int,help="Perform a dynamic step (reduce base_lr) when training has not improved after this many test iterations, default 10",default=10)
    parser.add_argument('--base_lr',type=float,help='Initial learning rate, default 0.01',default=0.01)
    parser.add_argument('--momentum',type=float,help="Momentum parameters, default 0.9",default=0.9)
    parser.add_argument('--weight_decay',type=float,help="Weight decay, default 0.001",default=0.001)
    parser.add_argument('--gamma',type=float,help="Gamma, default 0.001",default=0.001)
    parser.add_argument('--power',type=float,help="Power, default 1",default=1)
    parser.add_argument('--weights',type=str,help="Set of weights to initialize the model with")
    parser.add_argument('-p2','--prefix2',type=str,required=False,help="Second prefix for training/test files for combined training: <prefix>[train|test][num].types")
    parser.add_argument('-d2','--data_root2',type=str,required=False,help="Root folder for relative paths in second train/test files for combined training",default='')
    parser.add_argument('--data_ratio',type=float,required=False,help="Ratio to combine training data from 2 sources",default=None)
    return parser.parse_args(argv)


def check_file_exists(file):
    if not os.path.isfile(file):
        raise OSError('%s does not exist' % file)


def get_train_test_files(prefix, foldnums, allfolds, reduced, prefix2):
    files = {}
    for i in foldnums:
        files[i] = {}
        files[i]['train'] = '%strain%d.types' % (prefix, i)
        files[i]['test'] = '%stest%d.types' % (prefix, i)
        if reduced:
            files[i]['reduced_train'] = '%s_reducedtrain%d.types' % (prefix, i)
            files[i]['reduced_test'] = '%s_reducedtest%d.types' % (prefix, i)
        if prefix2:
            files[i]['train2'] = '%strain%d.types' % (prefix2, i)
            files[i]['test2'] = '%stest%d.types' % (prefix2, i)
            if reduced:
                files[i]['reduced_train2'] = '%s_reducedtrain%d.types' % (prefix2, i)
                files[i]['reduced_test2'] = '%s_reducedtest%d.types' % (prefix2, i)
    if allfolds:
        i = 'all'
        files[i] = {}
        files[i]['train'] = files[i]['test'] = '%s.types' % prefix
        if reduced:
            files[i]['reduced_train'] = files[i]['reduced_test'] = '%s_reduced.types' % prefix
        if prefix2:
            files[i]['train2'] = files[i]['test2'] = '%s.types' % prefix2
            if reduced:
                files[i]['reduced_train2'] = files[i]['reduced_test2'] = '%s_reduced.types' % prefix2
    for i in files:
        for file in files[i].values():
            check_file_exists(file)
    print(files)
    return files


if __name__ == '__main__':
    args = parse_args()

    #identify all train/test pairs
    try:
        train_test_files = get_train_test_files(args.prefix, args.foldnums, args.allfolds, args.reduced, args.prefix2)
    except OSError as e:
        print "error: %s" % e
        sys.exit(1)

    if len(train_test_files) == 0:
        print "error: missing train/test files"
        sys.exit(1)

    for i in train_test_files:
        print train_test_files[i]

    outprefix = args.outprefix
    if outprefix == '':
        outprefix = '%s.%d' % (os.path.splitext(os.path.basename(args.model))[0],os.getpid())

    mode = 'w'
    if args.cont:
        mode = 'a'
    
    test_aucs = []
    train_aucs = []
    test_rmsds = []
    train_rmsds = []
    all_y_true = []
    all_y_score = []
    all_y_aff = []
    all_y_predaff = []

    #train each pair
    for i in train_test_files:

        outname = '%s.%s' % (outprefix, i)
	print(outname)
        results = train_and_test_model(args, train_test_files[i], outname)
	


        if i == 'all': #only want crossval results
            continue

        if args.prefix2:
            test_vals, train_vals, test2_vals, train2_vals = results
        else:
            test_vals, train_vals = results

        all_y_true.extend(test_vals['y_true'])
        #all_y_score.extend(test_vals['y_score'])
        all_y_aff.extend(test_vals['y_aff'])
        all_y_predaff.extend(test_vals['y_predaff'])

        test_aucs.append(test_vals['auc'])
        train_aucs.append(train_vals['auc'])
        if test_vals['rmsd'] and train_vals['rmsd']:
            test_rmsds.append(test_vals['rmsd'])
            train_rmsds.append(train_vals['rmsd'])

        #if np.mean(train_aucs) > 0:
            #y_true, y_score, auc = test_vals['y_true'], test_vals['y_score'], test_vals['auc'][-1]
            #write_finaltest_file('%s.auc.finaltest' % outname, y_true, y_score, '# AUC %f\n' % auc, mode)

        if test_rmsds:
            y_aff, y_predaff, rmsd = test_vals['y_aff'], test_vals['y_predaff'], test_vals['rmsd'][-1]
            write_finaltest_file('%s.rmsd.finaltest' % outname, y_aff, y_predaff, '# RMSD %f\n' % rmsd, mode)

        #if args.prefix2:
            #y_true, y_score, auc = test2_vals['y_true'], test2_vals['y_score'], test2_vals['auc'][-1]
            #write_finaltest_file('%s.auc2.finaltest' % outname, y_true, y_score, '# AUC %f\n' % auc, mode)

            if test_rmsds:
                y_aff, y_predaff, rmsd = test2_vals['y_aff'], test2_vals['y_predaff'], test2_vals['rmsd'][-1]
                write_finaltest_file('%s.rmsd2.finaltest' % outname, y_aff, y_predaff, '# RMSD %f\n' % rmsd, mode)

    #skip post processing if it's not a full crossvalidation
    if len(args.foldnums) <= 1:
        sys.exit(0)

    #average, min, max test AUC for last 1000 iterations
    last_iters = 1000
    avg_auc, max_auc, min_auc = last_iters_statistics(test_aucs, args.iterations, args.test_interval, last_iters)
    txt = 'For the last %s iterations:\nmean AUC=%.2f  max AUC=%.2f  min AUC=%.2f' % (last_iters, avg_auc, max_auc, min_auc)

    #due to early termination length of results may not be equivalent
    #test_aucs = np.array(zip(*test_aucs))
    #train_aucs = np.array(zip(*train_aucs))

    #average aucs across folds
    #mean_test_aucs = test_aucs.mean(axis=1)
    #mean_train_aucs = train_aucs.mean(axis=1)

    #write test and train aucs (mean and for each fold)
    with open('%s.test' % outprefix, mode) as out:
        for m, r in zip(mean_test_aucs, test_aucs):
            out.write('%s %s\n' % (m, ' '.join([str(x) for x in r])))

    with open('%s.train' % outprefix, mode) as out:
        for m, r in zip(mean_train_aucs, train_aucs):
            out.write('%s %s\n' % (m, ' '.join([str(x) for x in r])))    

    #training plot of mean auc across folds
    training_plot('%s_train.pdf' % outprefix, mean_train_aucs, mean_test_aucs)

    #roc curve for the last iteration - combine all tests
    #if len(np.unique(all_y_true)) > 1:
        #fpr, tpr, _ = sklearn.metrics.roc_curve(all_y_true, all_y_score)
        #auc = sklearn.metrics.roc_auc_score(all_y_true, all_y_score)
        #write_finaltest_file('%s.finaltest' % outprefix, all_y_true, all_y_score, '# AUC %f\n' % auc, mode)
        #plot_roc_curve('%s_roc.pdf' % outprefix, fpr, tpr, auc, txt)

    if test_rmsds:

        test_rmsds = np.array(zip(*test_rmsds))
        train_rmsds = np.array(zip(*train_rmsds))

        #average rmsds across folds
        mean_test_rmsds = test_rmsds.mean(axis=1)
        mean_train_rmsds = train_rmsds.mean(axis=1)
	
        #write test and train rmsds (mean and for each fold)
        with open('%s.rmsd.test' % outprefix, mode) as out:
            for m, r in zip(mean_test_rmsds, test_rmsds):
                out.write('%s %s\n' % (m, ' '.join([str(x) for x in r])))

        with open('%s.rmsd.train' % outprefix,mode) as out:
            for m, r in zip(mean_train_rmsds, train_rmsds):
                out.write('%s %s \n' % (m, ' '.join([str(x) for x in r])))

        #training plot of mean rmsd across folds
        training_plot('%s_rmsd_train.pdf' % outprefix, mean_train_rmsds, mean_test_rmsds)

        all_y_aff = np.array(all_y_aff)
        all_y_predaff = np.array(all_y_predaff)
        yt = np.array(all_y_true, dtype=np.bool)
        rmsdt = sklearn.metrics.mean_squared_error(all_y_aff[yt], all_y_predaff[yt])
        r2t = sklearn.metrics.r2_score(all_y_aff[yt], all_y_predaff[yt])
        write_finaltest_file('%s.rmsd.finaltest' % outprefix, all_y_aff, all_y_predaff, '# RMSD,R^2 %f %f\n' % (rmsdt, r2t), mode)

        plot_correlation('%s_rmsd.pdf' % outprefix, all_y_aff[yt], all_y_predaff[yt], rmsdt, r2t)

