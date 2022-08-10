'''
Tuning or training with auxiliary OOD training data by classification resampling
With OOD quantile
'''

import copy
import time
import random
import argparse
import numpy as np
from pathlib import Path
# from functools import partial
from sklearn.covariance import EmpiricalCovariance

import torch
from torch import nn 
import torch.nn.functional as F
import torch.backends.cudnn as cudnn
from torch.utils.data import Subset, DataLoader

from models import get_clf
from trainers import get_trainer
from utils import setup_logger
from datasets import get_ds_info, get_ds_trf, get_ood_trf, get_ds

def init_seeds(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)

# scheduler
def cosine_annealing(step, total_steps, lr_max, lr_min):
    return lr_min + (lr_max - lr_min) * 0.5 * (1 + np.cos(step / total_steps * np.pi))

def sample_estimator(data_loader, clf, num_classes):
    clf.eval()
    group_lasso = EmpiricalCovariance(assume_centered=False)

    num_sample_per_class = np.zeros(num_classes)
    list_features = [0] * num_classes

    for sample in data_loader:
        data = sample['data'].cuda()
        target = sample['label'].cuda()

        with torch.no_grad():
            _, penulti_feature = clf(data, ret_feat=True)
        
        # construct the sample matrix
        for i in range(target.size(0)):
            label = target[i]
            if num_sample_per_class[label] == 0:
                list_features[label] = penulti_feature[i].view(1, -1)
            else:
                list_features[label] = torch.cat((list_features[label], penulti_feature[i].view(1, -1)), 0)
            num_sample_per_class[label] += 1

    category_sample_mean = []
    for j in range(num_classes):
        category_sample_mean.append(torch.mean(list_features[j], 0))

    X = 0
    for j in range(num_classes):
        if j == 0:
            X = list_features[j] - category_sample_mean[j]
        else:
            X = torch.cat((X, list_features[j] - category_sample_mean[j]), 0)

    # find inverse
    group_lasso.fit(X.cpu().numpy())
    precision = group_lasso.precision_
    
    return category_sample_mean, torch.from_numpy(precision).float().cuda()

def get_cond_maha_weight(data_loader, clf, num_classes, sample_mean, precision):
    clf.eval()
    min_mahas, full_mahas = [], []

    for sample in data_loader:
        data = sample['data'].cuda()

        with torch.no_grad():
            _, penul_feat = clf(data, ret_feat=True)

        # compute class conditional density
        # gaussian_score = 0
        maha_score = 0
        for j in range(num_classes):
            category_sample_mean = sample_mean[j]
            zero_f = penul_feat - category_sample_mean
            
            maha_dis = torch.mm(torch.mm(zero_f, precision), zero_f.t()).diag()
            if j == 0:
                maha_score = maha_dis.view(-1, 1)
            else:
                maha_score = torch.cat((maha_score, maha_dis.view(-1, 1)), dim=1)
    
        # add to list
        maha_score = torch.sqrt(maha_score)
        full_mahas.extend(maha_score.tolist())
        maha_min = torch.min(maha_score, dim=1)[0]
        min_mahas.extend(maha_min.tolist())

    return torch.tensor(min_mahas), torch.tensor(full_mahas)

def get_cond_negative_logit_weight(data_loader, clf):
    clf.eval()
    max_logits, full_logits = [], []

    for sample in data_loader:
        data = sample['data'].cuda()

        with torch.no_grad():
            logit = clf(data)
            full_logits.extend(logit.tolist())
            max_logit = torch.max(logit, dim=1)[0]
            max_logits.extend(max_logit.tolist())
    
    return -1.0 * torch.tensor(max_logits), -1.0 * torch.tensor(full_logits)

def get_abs_logit_weight(data_loader, clf):
    clf.eval()
    abs_scores = []

    for sample in data_loader:
        data = sample['data'].cuda()

        with torch.no_grad():
            logit = clf(data)
        
        abs_scores.extend(logit[:, -1].tolist())
    
    return torch.tensor(abs_scores)

def test(data_loader, net, num_classes):
    net.eval()

    total, correct = 0, 0
    total_loss = 0.0

    with torch.no_grad():
        for sample in data_loader:
            data = sample['data'].cuda()
            target = sample['label'].cuda()

            # forward
            logit = net(data)
            total_loss += F.cross_entropy(logit, target).item()

            _, pred = logit[:, :num_classes].max(dim=1)
            correct += pred.eq(target).sum().item()
            total += target.size(0)

    # average on sample
    print('[cla loss: {:.8f} | cla acc: {:.4f}%]'.format(total_loss / len(data_loader), 100. * correct / total))
    return {
        'cla_loss': total_loss / len(data_loader),
        'cla_acc': 100. * correct / total
    }

def main(args):
    init_seeds(args.seed)

    exp_path = Path(args.output_dir) / (args.id + '-' + args.ood) / '-'.join([args.arch, args.clf_type, args.training, args.dist, 'b_'+str(args.beta), 'q_'+str(args.ood_quantile)])
    
    print('>>> Output dir: {}'.format(str(exp_path)))
    exp_path.mkdir(parents=True, exist_ok=True)

    setup_logger(str(exp_path), 'console.log')

    train_trf_id = get_ds_trf(args.id, 'train')
    test_trf_id = get_ds_trf(args.id, 'test')

    train_set_id = get_ds(root=args.data_dir, ds_name=args.id, split='train', transform=train_trf_id)
    train_set_id_test = get_ds(root=args.data_dir, ds_name=args.id, split='train', transform=test_trf_id)
    test_set = get_ds(root=args.data_dir, ds_name=args.id, split='test', transform=test_trf_id)

    train_loader_id = DataLoader(train_set_id, batch_size=args.batch_size, shuffle=True, num_workers=args.prefetch, pin_memory=True)
    train_loader_id_test = DataLoader(train_set_id_test, batch_size=args.batch_size, shuffle=False, num_workers=args.prefetch, pin_memory=True)
    test_loader = DataLoader(test_set, batch_size=args.batch_size, shuffle=False, num_workers=args.prefetch, pin_memory=True)

    print('>>> ID: {} - OOD: {}'.format(args.id, args.ood))

    num_classes = len(get_ds_info(args.id, 'classes'))
    print('>>> CLF: {}'.format(args.arch))
    if args.training in ['uni', 'energy']:
        clf = get_clf(args.arch, num_classes, args.clf_type)
    elif args.training == 'abs':
        clf = get_clf(args.arch, num_classes+1, args.clf_type)
    clf = nn.DataParallel(clf)

    # move CLF to gpus
    gpu_idx = int(args.gpu_idx)
    if torch.cuda.is_available():
        torch.cuda.set_device(gpu_idx)
        clf.cuda()
        torch.cuda.manual_seed_all(args.seed)
    cudnn.benchmark = True

    # training parameters
    parameters, linear_parameters = [], []
    for name, parameter in clf.named_parameters():
        if name == 'module.linear.weight' or name == 'module.linear.bias':
            linear_parameters.append(parameter)
        else:
            parameters.append(parameter)
    
    print('LR: {:.2f} - WD: {:.5f} - LWD: {:.5f} - LMS: {}'.format(args.lr, args.weight_decay, args.linear_weight_decay, args.lr_stones))
    trainer = get_trainer(args.training)
    lr_stones = [int(args.epochs * float(lr_stone)) for lr_stone in args.lr_stones]
    optimizer = torch.optim.SGD(parameters, lr=args.lr, weight_decay=args.weight_decay, momentum=args.momentum)
    scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=lr_stones, gamma=0.1)
    linear_optimizer = torch.optim.SGD(linear_parameters, lr=args.lr, weight_decay=args.linear_weight_decay, momentum=args.momentum) # no weight_decay
    linear_scheduler = torch.optim.lr_scheduler.MultiStepLR(linear_optimizer, milestones=lr_stones, gamma=0.1)

    begin_time = time.time()
    start_epoch = 1
    cla_acc = 0.0

    train_trf_ood = get_ood_trf(args.id, args.ood, 'train')
    test_trf_ood = get_ood_trf(args.id, args.ood, 'test')
    train_all_set_ood = get_ds(root=args.data_dir, ds_name=args.ood, split='wo_cifar', transform=train_trf_ood)
    train_all_set_ood_test = get_ds(root=args.data_dir, ds_name=args.ood, split='wo_cifar', transform=test_trf_ood)

    for epoch in range(start_epoch, args.epochs+1):

        indices_candidate_ood = torch.randperm(len(train_all_set_ood))[:args.candidate_ood_size].tolist()
        train_candidate_set_ood_test = Subset(train_all_set_ood_test, indices_candidate_ood)
        train_candidate_loader_ood_test = DataLoader(train_candidate_set_ood_test, batch_size=args.batch_size_ood, shuffle=False, num_workers=args.prefetch, pin_memory=True)

        if args.dist == 'maha':
            cat_mean, precision = sample_estimator(train_loader_id_test, clf, num_classes)
            weights_candidate_ood, _ = get_cond_maha_weight(train_candidate_loader_ood_test, clf, num_classes, cat_mean, precision)
        elif args.dist == 'negative_logit':
            if args.training in ['uni', 'energy']:
                weights_candidate_ood, _ = get_cond_negative_logit_weight(train_candidate_loader_ood_test, clf)
            elif args.training == 'abs':
                weights_candidate_ood = get_abs_logit_weight(train_candidate_loader_ood_test, clf)
        else:
            raise RuntimeError('<<< invalid distance weights {}'.format(args.dist))
        
        # sort then quantile ascending
        idxs_sorted = np.argsort(weights_candidate_ood)
        spt = int(args.candidate_ood_size * args.ood_quantile)
        idxs_sampled = idxs_sorted[spt:spt + args.sampled_ood_size_factor * len(train_set_id)]
        
        indices_sampled_ood = [indices_candidate_ood[idx_sampled] for idx_sampled in idxs_sampled]
        train_set_ood = Subset(train_all_set_ood, indices_sampled_ood)
        train_loader_ood = DataLoader(train_set_ood, batch_size=args.sampled_ood_size_factor * args.batch_size, shuffle=True, num_workers=args.prefetch, pin_memory=True)

        trainer(train_loader_id, train_loader_ood, clf, optimizer, linear_optimizer)

        scheduler.step()
        linear_scheduler.step()
        val_metrics  = test(test_loader, clf, num_classes)
        cla_acc = val_metrics['cla_acc']

        print(
            '---> Epoch {:4d} | Time {:6d}s'.format(
                epoch,
                int(time.time() - begin_time)
            ),
            flush=True
        )

    torch.save({
        'epoch': epoch,
        'arch': args.arch,
        'state_dict': copy.deepcopy(clf.state_dict()),
        'cla_acc': cla_acc
    }, str(exp_path / 'cla_last.pth'))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Outlier Exposure')
    parser.add_argument('--seed', default=42, type=int, help='seed for init training')
    parser.add_argument('--data_dir', help='directory to store datasets', default='/data/cv')
    parser.add_argument('--id', type=str, default='cifar10')
    parser.add_argument('--ood', type=str, default='tiny_images')
    parser.add_argument('--training', type=str, default='uni', choices=['abs', 'uni', 'energy'])
    parser.add_argument('--beta', type=float, default=0.5)
    parser.add_argument('--clf_type', type=str, default='euclidean', choices=['euclidean', 'inner'])
    parser.add_argument('--dist', type=str, default='negative_logit', choices=['negative_logit', 'maha'])
    parser.add_argument('--ood_quantile', type=float, default=0.125)
    parser.add_argument('--output_dir', help='dir to store experiment artifacts', default='outputs')
    parser.add_argument('--arch', type=str, default='wrn40')
    parser.add_argument('--lr', type=float, default=0.1)
    parser.add_argument('--weight_decay', type=float, default=0.0001)
    parser.add_argument('--linear_weight_decay', type=float, default=0.0)
    parser.add_argument('--lr_stones', nargs='+', default=[1.0])
    parser.add_argument('--momentum', type=float, default=0.9)
    parser.add_argument('--epochs', type=int, default=100)
    parser.add_argument('--batch_size', type=int, default=64)
    parser.add_argument('--batch_size_ood', type=int, default=3072)
    parser.add_argument('--candidate_ood_size', type=int, default=2 ** 20)
    parser.add_argument('--sampled_ood_size_factor', type=int, default=2)
    parser.add_argument('--prefetch', type=int, default=16, help='number of dataloader workers')
    parser.add_argument('--gpu_idx', help='used gpu idx', type=int, default=0)
    args = parser.parse_args()
    
    main(args)