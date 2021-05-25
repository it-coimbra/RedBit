import torch
import torch.nn as nn
import torch.optim as optim
import torchvision
import torchvision.transforms as transforms
import torchvision.datasets as datasets
import numpy
from collections import namedtuple
import torch.backends.cudnn as cudnn
import gc
import os
from shutil import copyfile

from tools import RunManager_i, initial_scales, quantize, clear, get_grads, str2bool, accuracy, renameBestModel_i
from models import ResNet_imagenet, AlexNet, VGG

import argparse


def train(runManager, t):
	runManager.network.train()    #it tells pytorch we are in training mode
	
	runManager.begin_epoch()
	for images, targets in runManager.data_loader:
		images, targets = images.cuda(), targets.cuda()   #transfer to GPU
		
		optimizer, optimizer_fp, optimizer_sf = runManager.optimizer

		preds = runManager.network(images)                 #Forward pass
		loss = runManager.criterion(preds, targets)           #Calculate loss

		prec1, prec5 = accuracy(preds.data, targets, topk=(1, 5))
		runManager.epoch.loss.update(loss.data.item(), images.size(0))
		runManager.epoch.top1.update(prec1[0], images.size(0))
		runManager.epoch.top5.update(prec5[0], images.size(0))
		
		optimizer.zero_grad()
		optimizer_fp.zero_grad()
		optimizer_sf.zero_grad()
		# compute grads for quantized model
		loss.backward()

		# get all quantized kernels
		q_kernels = optimizer.param_groups[0]['params']

		# get their full precision backups
		fp_kernels = optimizer_fp.param_groups[0]['params']

		# get two scaling factors for each quantized kernel
		scaling_factors = optimizer_sf.param_groups[0]['params']

		for i in range(len(q_kernels)):

			# get a quantized kernel
			k = q_kernels[i]

			# get corresponding full precision kernel
			k_fp = fp_kernels[i]

			# get scaling factors for the quantized kernel
			f = scaling_factors[i]
			w_p, w_n = f.data[0], f.data[1]

			# get modified grads
			k_fp_grad, w_p_grad, w_n_grad = get_grads(k.grad.data, k_fp.data, w_p, w_n, t)

			# grad for full precision kernel
			k_fp.grad = k_fp_grad

			# we don't need to update the quantized kernel directly
			k.grad.data.zero_()

			# grad for scaling factors
			f.grad = torch.cuda.FloatTensor([w_p_grad, w_n_grad])

		# update all non quantized weights in quantized model
		# (usually, these are the last layer, the first layer, and all batch norm params)
		optimizer.step()

		# update all full precision kernels
		optimizer_fp.step()

		# update all scaling factors
		optimizer_sf.step()

		# update all quantized kernels with updated full precision kernels
		for i in range(len(q_kernels)):

			k = q_kernels[i]
			k_fp = fp_kernels[i]
			f = scaling_factors[i]
			w_p, w_n = f.data[0], f.data[1]

			# requantize a quantized kernel using updated full precision weights
			k.data = quantize(k_fp.data, w_p, w_n, t)

		gc.collect()
	runManager.end_epoch()


def val(runManager, best_acc, args):
	runManager.network.eval()   #it tells the model we are in validation mode
	
	runManager.begin_epoch()
	for images, targets in runManager.data_loader:
		images, targets = images.cuda(), targets.cuda()
		
		preds = runManager.network(images)
		loss = runManager.criterion(preds, targets)

		# measure accuracy and record loss
		prec1, prec5 = accuracy(preds.data, targets, topk=(1, 5))
		runManager.epoch.loss.update(loss.data.item(), images.size(0))
		runManager.epoch.top1.update(prec1[0], images.size(0))
		runManager.epoch.top5.update(prec5[0], images.size(0))

		gc.collect()

	runManager.end_epoch()

	#Save checkpoint
	if  torch.cuda.device_count() > 1 and args.distributed:
		torch.save({
			'epoch': runManager.epoch.count,
			'accuracy_top1': runManager.epoch.top1.avg,
			'accuracy_top5': runManager.epoch.top5.avg,
			'learning_rate': args.learning_rate,
			'network_state_dict': runManager.network.module.state_dict(),	# To generalize for loading later
			'optimizer_state_dict': runManager.optimizer[0].state_dict(),
			'optimizer_fp_state_dict': runManager.optimizer[1].state_dict(),
			'optimizer_sf_state_dict': runManager.optimizer[2].state_dict()
		}, f'trained_models/{args.networkCfg}.checkpoint.pth.tar')
	else:
		torch.save({
			'epoch': runManager.epoch.count,
			'accuracy_top1': runManager.epoch.top1.avg,
			'accuracy_top5': runManager.epoch.top5.avg,
			'learning_rate': args.learning_rate,
			'network_state_dict': runManager.network.state_dict(),
			'optimizer_state_dict': runManager.optimizer[0].state_dict(),
			'optimizer_fp_state_dict': runManager.optimizer[1].state_dict(),
			'optimizer_sf_state_dict': runManager.optimizer[2].state_dict()
		}, f'trained_models/{args.networkCfg}.checkpoint.pth.tar')

	# Save best network model
	if runManager.epoch.top1.avg >= best_acc[0]:
		best_acc[0] = runManager.epoch.top1.avg
		best_acc[1] = runManager.epoch.top5.avg
		copyfile(src=f'trained_models/{args.networkCfg}.checkpoint.pth.tar', 
				dst=f'trained_models/{args.networkCfg}.best.pth.tar')
	
	return best_acc

def adjust_learning_rate(optimizer, epoch, lr):
	"""Learning rate decays by 10 every 30 epochs until 90 epochs"""
	update_list = [30, 60, 90]
	new_lr=lr
	if epoch in update_list:
		new_lr = lr*0.1
		for opt in optimizer:
			for param_group in opt.param_groups:
				param_group['lr'] = new_lr

	return new_lr

#Parser################################################################

parser = argparse.ArgumentParser()
parser.add_argument('-n', '--network', default='ResNet',
					help='Network model to use (ResNet or AlexNet or VGG)')

parser.add_argument('-l', '--layers', type=int, default= '20',
					help='Number of layers')

parser.add_argument('-o', '--optimizer', default='Adam',
					help='Optimizer')

parser.add_argument('-lr', '--learning_rate', type=float, default='1e-3',
					help='Initial Learning Rate')

parser.add_argument('-wd', '--weight_decay', type=float, default='1e-4',
					help='Optimizer parameter weight decay')

parser.add_argument('-m', '--momentum', type=float, default='0.9',
					help='SGD momentum parameter')

parser.add_argument('-bs', '--batch_size', type=int, default='128',
					help='Batch Size')

parser.add_argument('-e', '--epochs', type=int, default='100',
					help='Number of epochs to train')

parser.add_argument('-nw', '--number_workers', type=int, default='4',
					help='Number of workers in DataLoader')

parser.add_argument('-bn', '--batch_norm', type=str2bool, nargs='?', const=True, default=True,
					help='If True, Batch Normalization is used (applicable for VGG; default=True)')

parser.add_argument('-lc', '--load_checkpoint', type=str2bool, nargs='?', const=True, default=False,
					help='To resume training, set to True')

parser.add_argument('-d', '--distributed', type=str2bool, nargs='?', const=True, default=True,
                    help='If True, DataParallel will be used to train on multiple GPUs')

#######################################################################


if __name__ == "__main__":
	if not os.path.exists('trained_models'):
		os.makedirs('trained_models')
	
	cudnn.benchmark = True

	HYPERPARAMETER_T = 0.05  # hyperparameter for quantization

	#Parse arguments
	args = parser.parse_args()

	######################################
	##              Datasets            ##
	######################################
	train_set = datasets.ImageFolder(
		root= '../datasets/ImageNet/train',
		transform=transforms.Compose([
			transforms.Resize((256, 256)),
			transforms.RandomCrop(227),
			transforms.RandomHorizontalFlip(),
			transforms.ToTensor(),
			transforms.Normalize(mean=[0.485, 0.456, 0.406],
				std=[1./255., 1./255., 1./255.])    
		])
	)

	val_set = datasets.ImageFolder(
		root= '../datasets/ImageNet/val',
		transform=transforms.Compose([
		transforms.Resize((256, 256)),
		transforms.CenterCrop(227),
		transforms.ToTensor(),
		transforms.Normalize(mean=[0.485, 0.456, 0.406],
			std=[1./255., 1./255., 1./255.])    
		])
	)

	Params = namedtuple('Params',['lr','batch_size','number_workers'])
	params = Params( args.learning_rate, args.batch_size, args.number_workers)

	######################################
	##            Dataloaders           ##
	######################################
	train_loader = torch.utils.data.DataLoader(
		train_set,
		batch_size=args.batch_size,
		shuffle=True,
		num_workers=args.number_workers,
		pin_memory=True
	)
	val_loader = torch.utils.data.DataLoader(
		val_set,
		batch_size=args.batch_size,
		shuffle=False,
		num_workers=args.number_workers,
		pin_memory=True
	)

	model_name = {11: 'VGG11', 13: 'VGG13', 16: 'VGG16', 19: 'VGG19'}
	# Network model
	if args.network.lower() == 'resnet':
		network = ResNet_imagenet(layers=args.layers).cuda()
	elif args.network.lower() == 'vgg' and args.layers in (11, 13, 16, 19):
		network = VGG(model_name[args.layers], num_classes=1000, batch_norm=args.batch_norm).cuda()
	elif args.network.lower() == 'alexnet':
		network = AlexNet().cuda()
	else:
		print('Error creating network (network and or configuration not supported)')
		exit()

	# All trainable parameters
	all_params = []
	count_targets = 0
	for n, p in network.named_parameters():
		if 'conv' in n or 'linear' in n:
			count_targets += 1
		all_params.append(p)

	# Parameters to be quantized (q_params) and respective fp copy (fp_params) and, finally, the 
	# rest of the trainable parameters (o_params) (first and last convolutional/linear layers removed)
	start_range = 1						# to remove first layer
	end_range = count_targets-2			# to remove last layer
	t_range = numpy.linspace(start_range, end_range, end_range-start_range+1).astype('int').tolist()
	q_params = []
	fp_params = []
	o_params = []
	index = -1
	for n, p in network.named_parameters():
		if 'conv' in n or 'linear' in n:
			index = index + 1
			if index in t_range:
				q_params.append(p)
				fp_params.append(p.data.clone().requires_grad_())
		else:
			o_params.append(p)

	# scaling factors for each quantized layer
	initial_scaling_factors = []
	for k, k_fp in zip(q_params, fp_params):
		# choose initial scaling factors 
		w_p_initial, w_n_initial = initial_scales(k_fp.data)
		initial_scaling_factors += [(w_p_initial, w_n_initial)]
		
		# do quantization
		k.data = quantize(k_fp.data, w_p_initial, w_n_initial, t=HYPERPARAMETER_T)
		
	#All trainable parameters:
	parameters = [
		{'params': q_params},
		{'params': fp_params},
		{'params': o_params}
	]
	# Optimizer
	if args.optimizer.lower() == 'adam':
		optimizer = optim.Adam(parameters, lr=args.learning_rate, weight_decay=args.weight_decay)
	elif args.optimizer.lower() == 'sgd':
		optimizer = optim.SGD(parameters, lr=args.learning_rate, momentum=args.momentum, weight_decay=args.weight_decay)
	else:
		print("Unsupported optimizer (Use Adam or SGD)")
		exit()
	
	# optimizer for updating only all_fp_kernels
	if args.optimizer.lower() == 'adam':
		optimizer_fp = optim.Adam(fp_params, lr=args.learning_rate)
	elif args.optimizer.lower() == 'sgd':
		optimizer_fp = optim.SGD(fp_params, lr=args.learning_rate)

	# optimizer for updating only scaling factors
	if args.optimizer.lower() == 'adam':
		optimizer_sf = optim.Adam([
			torch.cuda.FloatTensor([w_p, w_n])
			for w_p, w_n in initial_scaling_factors
		], lr=args.learning_rate)
	elif args.optimizer.lower() == 'sgd':
		optimizer_sf = optim.SGD([
			torch.cuda.FloatTensor([w_p, w_n])
			for w_p, w_n in initial_scaling_factors
		], lr=args.learning_rate)
	
	#List of optimizers
	optimizer_list=[optimizer, optimizer_fp, optimizer_sf]

	# Loss function
	criterion = nn.CrossEntropyLoss().cuda()

	if args.batch_norm and args.network.lower() == 'vgg':
		bn = '.BN'
	else:
		bn = ''

	if args.network.lower() in ('resnet', 'vgg'):
		args.networkCfg = f'ImageNet.{args.network}-{args.layers}{bn}.TTQ.{args.optimizer}.LR{args.learning_rate}'
	elif args.network.lower() == 'alexnet':
		args.networkCfg = f'ImageNet.{args.network}.TTQ.{args.optimizer}.LR{args.learning_rate}'

	# Resume training
	if args.load_checkpoint:
		checkpoint = torch.load(f'trained_models/{args.networkCfg}.checkpoint.pth.tar')
		network.load_state_dict(checkpoint['network_state_dict'])
		optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
		optimizer_fp.load_state_dict(checkpoint['optimizer_fp_state_dict'])
		optimizer_sf.load_state_dict(checkpoint['optimizer_sf_state_dict'])
		start_epoch = checkpoint['epoch']	# Training will start in epoch+1
		best_acc = [0.0,0.0]
		best_acc[0] = checkpoint['accuracy_top1']
		best_acc[1] = checkpoint['accuracy_top1']
		lr = checkpoint['learning_rate']
	else:
		lr=args.learning_rate
		start_epoch=0       # Training will start in 0+1
		best_acc = [0.0,0.0]

	if torch.cuda.device_count() > 1 and args.distributed == True:
		network = nn.DataParallel(network)

	trainManager = RunManager_i(f'{args.networkCfg}.Train', 'Train')
	validationManager = RunManager_i(f'{args.networkCfg}.Validation', 'Validation')

	trainManager.begin_run(params, network, train_loader, criterion, optimizer_list, start_epoch)
	validationManager.begin_run(params, network, val_loader, criterion, optimizer_list, start_epoch)
	for epoch in range(start_epoch, args.epochs):
		lr=adjust_learning_rate(optimizer_list, epoch, lr)
		trainManager.lr=lr
		validationManager.lr=lr
		args.learning_rate = lr
		train(trainManager, HYPERPARAMETER_T)
		best_acc = val(validationManager, best_acc, args)
		clear()
		trainManager.printDF()
		validationManager.printDF()
		print(f'Best accuracy: {best_acc[0]:.2f}% top-1; {best_acc[1]:.2f}% top-5')
		
	trainManager.end_run()
	validationManager.end_run()
	renameBestModel_i(args, best_acc)

