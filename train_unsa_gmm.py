'''
Tuning or training with auxiliary OOD training data by classification resampling
'''

import copy
from math import ceil
import time
import random
import argparse
import numpy as np
from pathlib import Path
from scipy.stats import norm
import matplotlib.pyplot as plt
from sklearn.mixture import GaussianMixture as GMM

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

# OOD samples have larger weight
def get_energy_weight(data_loader, clf):
    clf.eval()
    
    energy_weight = []

    for sample in data_loader:
        data = sample['data'].cuda()
        
        with torch.no_grad():
            logit = clf(data)
            energy_weight.extend((-torch.logsumexp(logit, dim=1)).tolist())
    
    return energy_weight

weight_dic = {
    'energy': get_energy_weight
}

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

    # exp_path = Path(args.output_dir) / (args.id + '-' + args.ood) / '-'.join([args.arch, args.training, args.scheduler, 'gmm', 'b_'+str(args.beta), 'wp_'+str(args.warmup), 'c1e_'+str(args.c1e), 'c2e_'+str(args.c2e), 'c3e_'+str(args.c3e)])
    # exp_path = Path(args.output_dir) / (args.id + '-' + args.ood) / '-'.join([args.arch, args.training, args.scheduler, 'gmm', 'b_'+str(args.beta), 'lc_start'+str(args.lc_start)])
    # exp_path = Path(args.output_dir) / (args.id + '-' + args.ood) / '-'.join([args.arch, args.training, args.scheduler, 'gmm', 'b_'+str(args.beta), 'wp_'+str(args.warmup), 'c1e_'+str(args.c1e), 'c2e_min_'+str(args.c2e_min), 'c2e_max_'+str(args.c2e_max), 'c3e_'+str(args.c3e)])
    exp_path = Path(args.output_dir) / (args.id + '-' + args.ood) / '-'.join([args.arch, args.training, args.scheduler, 'gmm', 'b_'+str(args.beta), 'wp_'+str(args.warmup), 'w1_w3_w2'])

    exp_path.mkdir(parents=True, exist_ok=True)

    setup_logger(str(exp_path), 'console.log')
    print('>>> Output dir: {}'.format(str(exp_path)))
    
    train_trf_id = get_ds_trf(args.id, 'train')
    test_trf_id = get_ds_trf(args.id, 'test')

    train_set_id = get_ds(root=args.data_dir, ds_name=args.id, split='train', transform=train_trf_id)
    test_set = get_ds(root=args.data_dir, ds_name=args.id, split='test', transform=test_trf_id)

    train_loader_id = DataLoader(train_set_id, batch_size=args.batch_size, shuffle=True, num_workers=args.prefetch, pin_memory=True)
    test_loader = DataLoader(test_set, batch_size=args.batch_size, shuffle=False, num_workers=args.prefetch, pin_memory=True)

    print('>>> ID: {} - OOD: {}'.format(args.id, args.ood))
    num_classes = len(get_ds_info(args.id, 'classes'))
    print('>>> CLF: {}'.format(args.arch))
    if args.training in ['uni', 'energy', 'binary']:
        clf = get_clf(args.arch, num_classes, args.include_binary)
    elif args.training == 'abs':
        clf = get_clf(args.arch, num_classes+1, args.include_binary)
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
        if name in ['module.linear.weight', 'module.linear.bias', 'module.binary_linear.weight', 'module.binary_linear.bias']:
            linear_parameters.append(parameter)
        else:
            parameters.append(parameter)
    
    print('Optimizer: LR: {:.2f} - WD: {:.5f} - LWD: {:.5f} - Mom: {:.2f} - Nes: True'.format(args.lr, args.weight_decay, args.linear_weight_decay, args.momentum))
    trainer = get_trainer(args.training)
    get_weight = weight_dic[args.weighting]
    lr_stones = [int(args.epochs * float(lr_stone)) for lr_stone in args.lr_stones]
    optimizer = torch.optim.SGD(parameters, lr=args.lr, weight_decay=args.weight_decay, momentum=args.momentum, nesterov=True)
    linear_optimizer = torch.optim.SGD(linear_parameters, lr=args.lr, weight_decay=args.linear_weight_decay, momentum=args.momentum, nesterov=True)
    
    if args.scheduler == 'multistep':
        print('Scheduler: MultiStepLR - LMS: {}'.format(args.lr_stones))
        scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=lr_stones, gamma=0.1)
        linear_scheduler = torch.optim.lr_scheduler.MultiStepLR(linear_optimizer, milestones=lr_stones, gamma=0.1)
    elif args.scheduler == 'lambda':
        print('Scheduler: LambdaLR')
        scheduler = torch.optim.lr_scheduler.LambdaLR(
            optimizer,
            lr_lambda=lambda step: cosine_annealing(
                step,
                args.epochs * len(train_loader_id),
                1,
                1e-6 / args.lr
            )
        )
        linear_scheduler = torch.optim.lr_scheduler.LambdaLR(
            linear_optimizer,
            lr_lambda=lambda step: cosine_annealing(
                step,
                args.epochs * len(train_loader_id),
                1,
                1e-6 / args.lr
            )
        )
    else:
        raise RuntimeError('<<< Invalid scheduler: {}'.format(args.scheduler))
    
    begin_time = time.time()
    start_epoch = 1
    cla_acc = 0.0

    train_trf_ood = get_ood_trf(args.id, args.ood, 'train')
    test_trf_ood = get_ood_trf(args.id, args.ood, 'test')
    train_all_set_ood = get_ds(root=args.data_dir, ds_name=args.ood, split='wo_cifar', transform=train_trf_ood)
    train_all_set_ood_test = get_ds(root=args.data_dir, ds_name=args.ood, split='wo_cifar', transform=test_trf_ood)

    plt.figure(figsize=(100, 100), dpi=100)
    for epoch in range(start_epoch, args.epochs+1):
        
        indices_candidate_ood = torch.randperm(len(train_all_set_ood))[:args.candidate_ood_size].tolist()
        train_candidate_set_ood_test = Subset(train_all_set_ood_test, indices_candidate_ood)
        train_candidate_loader_ood_test = DataLoader(train_candidate_set_ood_test, batch_size=args.batch_size_ood, shuffle=False, num_workers=args.prefetch, pin_memory=True)

        if epoch > args.warmup:
            weights_candidate_ood = np.asarray(get_weight(train_candidate_loader_ood_test, clf))

            bin_counts, bins = np.histogram(weights_candidate_ood, bins=100) # true OOD data weights distribution
            bin_probs = bin_counts / len(weights_candidate_ood)

            gmm = GMM(n_components=3).fit(weights_candidate_ood.reshape(-1, 1))
            m_g = gmm.means_
            c_g  = gmm.covariances_
            w_g = gmm.weights_

            g_seq = np.argsort(m_g, axis=0).squeeze()
            
            m1 = float(m_g[g_seq[0]][0])
            s1 = np.sqrt(float(c_g[g_seq[0]][0][0]))
            w1 = w_g[g_seq[0]]

            m2 = float(m_g[g_seq[1]][0])
            s2 = np.sqrt(float(c_g[g_seq[1]][0][0]))
            w2 = w_g[g_seq[1]]

            m3 = float(m_g[g_seq[2]][0])
            s3 = np.sqrt(float(c_g[g_seq[2]][0][0]))
            w3 = w_g[g_seq[2]]

            print(w1, w2, w3)
            estimated_bin_probs_c1 = norm.cdf(bins[1:], m1, s1) - norm.cdf(bins[:-1], m1, s1)
            estimated_bin_probs_c2 = norm.cdf(bins[1:], m2, s2) - norm.cdf(bins[:-1], m2, s2)
            estimated_bin_probs_c3 = norm.cdf(bins[1:], m3, s3) - norm.cdf(bins[:-1], m3, s3)
            # estimated_bin_probs_g = estimated_bin_probs_c1 * w1 + estimated_bin_probs_c2 * w2 +  estimated_bin_probs_c3 * w3
            # c2e = args.c2e_max - epoch / args.epochs * (args.c2e_max - args.c2e_min)
            # estimated_bin_probs_g_adj = estimated_bin_probs_c1 * w1 * args.c1e + estimated_bin_probs_c2 * w2 * args.c2e +  estimated_bin_probs_c3 * w3 * args.c3e
            # estimated_bin_probs_g_adj = estimated_bin_probs_c1 * w1 * args.c1e + estimated_bin_probs_c2 * w2 * c2e +  estimated_bin_probs_c3 * w3 * args.c3e
            estimated_bin_probs_g_adj = estimated_bin_probs_c1 * (w1 + w3) + estimated_bin_probs_c2 * w2

            estimated_bin_probs_c1 = [float(estimated_bin_prob_c1) / sum(estimated_bin_probs_c1) for estimated_bin_prob_c1 in estimated_bin_probs_c1] # normalize
            estimated_bin_probs_c2 = [float(estimated_bin_prob_c2) / sum(estimated_bin_probs_c2) for estimated_bin_prob_c2 in estimated_bin_probs_c2] # normalize
            estimated_bin_probs_c3 = [float(estimated_bin_prob_c3) / sum(estimated_bin_probs_c3) for estimated_bin_prob_c3 in estimated_bin_probs_c3] # normalize
            # estimated_bin_probs_g = [float(estimated_bin_prob_g) / sum(estimated_bin_probs_g) for estimated_bin_prob_g in estimated_bin_probs_g] # normalize
            estimated_bin_probs_g_adj = [float(estimated_bin_prob_g_adj) / sum(estimated_bin_probs_g_adj) for estimated_bin_prob_g_adj in estimated_bin_probs_g_adj] # normalize

            target_bin_probs = estimated_bin_probs_g_adj
            target_bin_counts = [ceil(args.sampled_ood_size_factor * len(train_set_id) * target_bin_prob) for target_bin_prob in target_bin_probs]
            
            plt.subplot(10, 10, epoch)
            plt.plot(bins[:-1], bin_probs, color='r', label='true bin probs')
            # plt.plot(bins[:-1], estimated_bin_probs_g, color='r', alpha=0.5)
            plt.plot(bins[:-1], target_bin_probs, color='b', linestyle='dashed', label='target bin probs', alpha=0.8)

            sep_line = True
            for bin_ in bins[::-1][1:]:
                if sep_line:
                    binary_ = torch.sigmoid(clf.module.binary_linear.weight.detach().cpu() * np.array(bin_)).squeeze().item()
                    if binary_ < 0.99:
                        plt.axvline(x=bin_, ls="-", c="green")
                        sep_line = False

            # sort confidence into corresponding bins
            bin_labels = np.digitize(weights_candidate_ood, bins)
            idxs_sampled = []
            for i, target_bin_count in enumerate(target_bin_counts):
                if target_bin_count > 0:
                    target_bin_idxs = np.where(bin_labels==i+1)[0]
                    if len(target_bin_idxs) == 0:
                        # random sampling
                        idxs_sampled.extend(random.sample(range(len(bin_labels)), k=target_bin_count))
                    else:
                        if len(target_bin_idxs) < target_bin_count:
                            idxs_sampled.extend(target_bin_idxs.tolist())
                            idxs_sampled.extend(random.sample(range(len(bin_labels)), k=target_bin_count-len(target_bin_idxs))) # random
                            # idxs_sampled.extend(random.choices(target_bin_idxs.tolist(), k=target_bin_count-len(target_bin_idxs))) # replacement
                        else:
                            idxs_sampled.extend(random.sample(target_bin_idxs.tolist(), k=target_bin_count))
            
            #  visualize the true target sampling distribution
            weights_target_ood = weights_candidate_ood[idxs_sampled]
            true_target_bin_counts, _ = np.histogram(weights_target_ood, bins)
            true_target_bin_probs = true_target_bin_counts / len(weights_target_ood)
            plt.plot(bins[:-1], true_target_bin_probs, color='g', linestyle='dashed', alpha=0.8)

            indices_sampled_ood = [indices_candidate_ood[idx_sampled] for idx_sampled in idxs_sampled]
        else:
            indices_sampled_ood = torch.randperm(len(train_all_set_ood))[:args.sampled_ood_size_factor * len(train_set_id)].tolist()

        print(len(indices_sampled_ood))

        train_set_ood = Subset(train_all_set_ood, indices_sampled_ood)
        train_loader_ood = DataLoader(train_set_ood, batch_size=args.sampled_ood_size_factor * args.batch_size, shuffle=True, num_workers=args.prefetch, pin_memory=True)

        if args.scheduler == 'multistep':
            trainer(train_loader_id, train_loader_ood, clf, optimizer, linear_optimizer, beta=args.beta)
            scheduler.step()
            linear_scheduler.step()
        elif args.scheduler == 'lambda':
            trainer(train_loader_id, train_loader_ood, clf, optimizer, linear_optimizer, scheduler, linear_scheduler, beta=args.beta)
        else:
            raise RuntimeError('<<< Invalid scheduler: {}'.format(args.scheduler))
        val_metrics = test(test_loader, clf, num_classes)
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
        }, str(exp_path / (str(epoch)+'.pth')))
    
    # save the figure
    fig_path = Path('./imgs') / args.fig_name
    plt.savefig(str(fig_path))

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
    parser.add_argument('--fig_name', type=str, default='test.png')
    parser.add_argument('--id', type=str, default='cifar10')
    parser.add_argument('--ood', type=str, default='tiny_images')
    parser.add_argument('--training', type=str, default='uni', choices=['uni', 'abs', 'energy', 'binary'])
    parser.add_argument('--weighting', type=str, default='msp', choices=['msp', 'abs', 'energy', 'binary'])
    parser.add_argument('--beta', type=float, default=2.0)
    parser.add_argument('--warmup', type=int, default=0)
    # parser.add_argument('--c1e', type=float, default=1.0)
    # parser.add_argument('--c2e', type=float, default=1.0)
    # parser.add_argument('--c2e_max', type=float, default=1.0)
    # parser.add_argument('--c2e_min', type=float, default=1.0)
    # parser.add_argument('--c3e', type=float, default=1.0)
    parser.add_argument('--include_binary', action='store_true')
    parser.add_argument('--output_dir', help='dir to store experiment artifacts', default='outputs')
    parser.add_argument('--arch', type=str, default='wrn40')
    parser.add_argument('--lr', type=float, default=0.1)
    parser.add_argument('--weight_decay', type=float, default=0.0001)
    parser.add_argument('--linear_weight_decay', type=float, default=0.0001)
    parser.add_argument('--scheduler', type=str, default='lambda', choices=['lambda', 'multistep'])
    parser.add_argument('--lr_stones', nargs='+', default=[0.5, 0.75, 0.9])
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