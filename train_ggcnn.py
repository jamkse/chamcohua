import datetime
import os
import sys
import argparse
import logging

import cv2
import time
import numpy as np

import random
import torch
import torch.utils.data
import torch.optim as optim

import tensorboardX
from torchsummary import summary

from utils.dataset_processing.evaluation import evaluation
from utils.saver import Saver
from models import get_network
from models.common import post_process_output
from models.loss import focal_loss
from utils.data.grasp_data import GraspDataset

logging.basicConfig(level=logging.INFO)


def parse_args():
    parser = argparse.ArgumentParser(description='Train ggcnn')
    parser.add_argument('--network', type=str, default='ggcnn2', choices=['ggcnn', 'ggcnn2'], help='Network Name in .models')
    # 数据集
    parser.add_argument('--dataset-path', default='D:/guyueju/code/cornell', type=str, help='数据集路径')
    # 训练超参数
    parser.add_argument('--batch-size', type=int, default=2, help='Batch size')
    parser.add_argument('--epochs', type=int, default=1000, help='Training epochs')
    parser.add_argument('--lr', type=float, default=1e-3, help='学习率')
    parser.add_argument('--weight-decay', type=float, default=0, help='权重衰减 L2正则化系数')
    parser.add_argument('--num-workers', type=int, default=2, help='Dataset workers')  # pytorch 线程
    # 抓取表示超参数
    parser.add_argument('--output-size', type=int, default=360, help='output size')
    # 保存地址
    parser.add_argument('--outdir', type=str, default='output', help='Training Output Directory')
    parser.add_argument('--modeldir', type=str, default='models', help='model保存地址')
    parser.add_argument('--logdir', type=str, default='tensorboard', help='summary保存文件夹')
    parser.add_argument('--imgdir', type=str, default='img', help='中间预测图保存文件夹')
    parser.add_argument('--max_models', type=int, default=3, help='最大保存的模型数')
    # device
    parser.add_argument('--device-name', type=str, default='cpu', choices=['cpu', 'cuda:0'], help='是否使用GPU')
    # description
    parser.add_argument('--description', type=str, default='wangdexin_test', help='Training description')
    # 从已有网络继续训练
    parser.add_argument('--goon-train', type=bool, default=False, help='是否从已有网络继续训练')
    parser.add_argument('--model', type=str, default='output/models/211128_1147_new/epoch_0145_acc_0.0000.pth', help='保存的模型')
    parser.add_argument('--start-epoch', type=int, default=146, help='继续训练开始的epoch')
    args = parser.parse_args()

    return args


def setup_seed(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    random.seed(seed)
    torch.backends.cudnn.deterministic = True


def validate(net, device, val_data, saver, args):
    """
    Run validation.
    :param net: 网络
    :param device:
    :param val_data: 验证数据集
    :param saver: 保存器
    :param args:
    :return: Successes, Failures and Losses
    """
    net.eval()

    results = {
        'accuracy': 0.0,
        'graspable': 0,
        'loss': 0,
        'losses': {
        }
    }

    ld = len(val_data)

    with torch.no_grad():     # 不计算梯度，不反向传播
        batch_idx = 0
        for x, y in val_data:
            batch_idx += 1
            print ("\r Validating... {:.2f}".format(batch_idx/ld), end="")

            # 预测并计算损失
            lossd = focal_loss(net, x.to(device), y[0].to(device), y[1].to(device), y[2].to(device), y[3].to(device))

            # 输出值预处理
            pos_out, ang_out, wid_out = post_process_output(lossd['pred']['pred_pos'], 
                                                                 lossd['pred']['pred_cos'], 
                                                                 lossd['pred']['pred_sin'],
                                                                 lossd['pred']['pred_wid'])
            results['graspable'] += np.max(pos_out) / ld

            # 评估
            ang_tar = torch.atan2(y[2], y[1]) / 2.0
            ret = evaluation(pos_out, ang_out, wid_out, y[0], ang_tar, y[3])
            results['accuracy'] += ret / ld
            
            # 统计损失
            loss = lossd['loss']    # 损失和
            results['loss'] += loss.item()/ld       # 损失累加
            for ln, l in lossd['losses'].items():   # 添加单项损失
                if ln not in results['losses']:
                    results['losses'][ln] = 0
                results['losses'][ln] += l.item()/ld

    return results


def train(epoch, net, device, train_data, optimizer):
    """
    Run one training epoch
    :param epoch: Current epoch
    :param net: Network
    :param device: Torch device
    :param optimizer: Optimizer
    :return:  Average Losses for Epoch
    """
    results = {
        'loss': 0,
        'losses': {
        }
    }

    net.train()

    batch_idx = 0
    sum_batch = len(train_data)
    for x, y in train_data:
        """
        x = (batch, 1, h, w)
        y = (batch, 1, h, w) (batch, 1, h, w) (batch, 1, h, w) (batch, 1, h, w) 
        """
        batch_idx += 1

        # 计算损失
        lossd = focal_loss(net, x.to(device), y[0].to(device), y[1].to(device), y[2].to(device), y[3].to(device))

        loss = lossd['loss']        # 损失和

        if batch_idx % 1 == 0:
            logging.info('Epoch: {}, '
                        'Batch: {}/{}, '
                        'loss_pos: {:.5f}, '
                        'loss_cos: {:.5f}, '
                        'loss_sin: {:.5f}, '
                        'loss_wid: {:.5f}, '
                        'Loss: {:0.5f}'.format(
                epoch, batch_idx, sum_batch,
                lossd['losses']['loss_pos'], lossd['losses']['loss_cos'], lossd['losses']['loss_sin'], lossd['losses']['loss_wid'], 
                loss.item()))

        # 统计损失
        results['loss'] += loss.item()
        for ln, l in lossd['losses'].items():
            if ln not in results['losses']:
                results['losses'][ln] = 0
            results['losses'][ln] += l.item()

        # 反向传播
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

    results['loss'] /= batch_idx    # 计算一个epoch的损失均值
    for l in results['losses']:
        results['losses'][l] /= batch_idx

    return results


def datasetloaders(Dataset, args):
    # 训练集
    train_dataset = Dataset(args.dataset_path,
                            start=0.0, 
                            end=0.01,
                            output_size=args.output_size,
                            argument=True)
    train_data = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=args.num_workers)

    # 部分训练集->验证
    train_val_dataset = Dataset(args.dataset_path,
                                start=0.0, 
                                end=0.01,
                                output_size=args.output_size,
                                argument=False)
    train_val_data = torch.utils.data.DataLoader(
        train_val_dataset, 
        batch_size=1,
        shuffle=False,
        num_workers=1)

    # 测试集
    val_dataset = Dataset(args.dataset_path,
                          start=0.99, 
                          end=1.0,
                          output_size=args.output_size,
                          argument=False)
    val_data = torch.utils.data.DataLoader(
        val_dataset, 
        batch_size=1,
        shuffle=False,
        num_workers=1)

    return train_data, train_val_data, val_data


def run():
    # 设置随机数种子
    # setup_seed(2)
    args = parse_args()

    # 设置保存器
    dt = datetime.datetime.now().strftime('%y%m%d_%H%M')
    net_desc = '{}_{}'.format(dt, '_'.join(args.description.split()))
    saver = Saver(args.outdir, args.logdir, args.modeldir, args.imgdir, net_desc)
    # 初始化tensorboard 保存器
    tb = saver.save_summary()

    # 加载数据集
    logging.info('Loading Dataset...')
    train_data, train_val_data, val_data = datasetloaders(GraspDataset, args)
    print('>> train dataset: {}'.format(len(train_data) * args.batch_size))
    print('>> train_val dataset: {}'.format(len(train_val_data)))
    print('>> test dataset: {}'.format(len(val_data)))

    # 加载网络
    logging.info('Loading Network...')
    ggcnn = get_network(args.network)
    net = ggcnn()
    device_name = args.device_name if torch.cuda.is_available() else "cpu"
    if args.goon_train:
        # 加载预训练模型
        pretrained_dict = torch.load(args.model, map_location=torch.device(device_name))
        net.load_state_dict(pretrained_dict, strict=True)   # True:完全吻合，False:只加载键值相同的参数，其他加载默认值。
    device = torch.device(device_name)      # 指定运行设备
    net = net.to(device)

    # 优化器
    optimizer = optim.Adam(net.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=[1000], gamma=0.5)     # 学习率衰减    20, 30, 60
    logging.info('optimizer Done')

    # 打印网络结构
    summary(net, (1, args.output_size, args.output_size))            # 将网络结构信息输出到终端
    saver.save_arch(net, (1, args.output_size, args.output_size))    # 保存至文件 output/arch.txt

    # 训练
    best_acc = 0.0
    start_epoch = args.start_epoch if args.goon_train else 0
    for _ in range(start_epoch):
        scheduler.step()
    for epoch in range(args.epochs)[start_epoch:]:
        logging.info('Beginning Epoch {:02d}, lr={}'.format(epoch, optimizer.state_dict()['param_groups'][0]['lr']))
        # 训练
        train_results = train(epoch, net, device, train_data, optimizer)
        scheduler.step()

        # 保存训练日志
        tb.add_scalar('train_loss/loss', train_results['loss'], epoch)
        for n, l in train_results['losses'].items():
            tb.add_scalar('train_loss/' + n, l, epoch)

        if epoch % 1 == 0:
            logging.info('>>> Validating...')

            # ====================== 使用测试集验证 ======================
            test_results = validate(net, device, val_data, saver, args)
            # 打印日志
            print('\n>>> test_graspable = {:.5f}'.format(test_results['graspable']))
            print('>>> test_accuracy: %f' % (test_results['accuracy']))
            # 保存测试集日志
            tb.add_scalar('test_pred/test_graspable', test_results['graspable'], epoch)
            tb.add_scalar('test_pred/test_accuracy', test_results['accuracy'], epoch)
            tb.add_scalar('test_loss/loss', test_results['loss'], epoch)
            for n, l in test_results['losses'].items():
                tb.add_scalar('test_loss/' + n, l, epoch)

            # ====================== 使用部分训练集进行验证 ======================
            train_val_results = validate(net, device, train_val_data, saver, args)

            print('\n>>> train_val_graspable = {:.5f}'.format(train_val_results['graspable']))
            print('>>> train_val_accuracy: %f' % (train_val_results['accuracy']))

            tb.add_scalar('train_val_pred/train_val_graspable', train_val_results['graspable'], epoch)
            tb.add_scalar('train_val_pred/train_val_accuracy', train_val_results['accuracy'], epoch)
            tb.add_scalar('train_val_loss/loss', train_val_results['loss'], epoch)
            for n, l in train_val_results['losses'].items():
                tb.add_scalar('train_val_loss/' + n, l, epoch)

            # 保存模型
            accuracy = test_results['accuracy']
            if accuracy >= best_acc :
                print('>>> save model: ', 'epoch_%04d_acc_%0.4f.pth' % (epoch, accuracy))
                saver.save_model(net, 'epoch_%04d_acc_%0.4f.pth' % (epoch, accuracy))
                best_acc = accuracy
            else:
                print('>>> save model: ', 'epoch_%04d_acc_%0.4f_.pth' % (epoch, accuracy))
                saver.save_model(net, 'epoch_%04d_acc_%0.4f_.pth' % (epoch, accuracy))
                saver.remove_model(args.max_models)  # 删除多余的旧模型

    tb.close()


if __name__ == '__main__':
    run()
