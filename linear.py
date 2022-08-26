import argparse
import logging
import os
import torch
from torch.utils.data import DataLoader
import numpy as np
import time
import datetime
import logging
from tqdm import tqdm

from utils import utils, hyperparameters
from utils.torch_mlp_clf import TorchMLPClassifier
import datasets
from model import ModelWrapper


MODELS = [
	'resnet50', 'resnet50_ReGP_NRF',
	'resnet18', 'resnet18_ReGP_NRF',
	'audiontt',
	'vit_base', 'vit_small', 'vit_tiny',
	'vitc_base', 'vitc_small', 'vitc_tiny',
]


@torch.no_grad()
def get_embeddings(model, data_loader, fp16_scaler):
	model.eval()
	embs, targets = [], []
	for data, target in tqdm(data_loader, desc='Extracting embeddings...'):
		with torch.cuda.amp.autocast(enabled=(fp16_scaler is not None)):
			if 'vit' in args.model_type:
				emb = utils.encode_vit(
					model.encoder,
					data.cuda(non_blocking=True),
					split_frames=True,
					use_cls=args.use_cls,
				)
			else:
				emb = model(data.cuda(non_blocking=True))
		if isinstance(emb, list):
			emb = emb[-1]
		emb = emb.detach().cpu().numpy()
		embs.extend(emb)
		targets.extend(target.numpy())

	return np.array(embs), np.array(targets)


def eval_linear(model, train_loader, val_loader, test_loader, use_fp16):

	# mixed precision
	fp16_scaler = None 
	if use_fp16:
		fp16_scaler = torch.cuda.amp.GradScaler()	

	print('Extracting embeddings')
	start = time.time()
	X_train, y_train = get_embeddings(model, train_loader, fp16_scaler)
	X_val, y_val = get_embeddings(model, val_loader, fp16_scaler)
	X_test, y_test = get_embeddings(model, test_loader, fp16_scaler)
	print(f'Done\tTime elapsed = {time.time() - start:.2f}s')

	print('Fitting linear classifier')
	start = time.time()
	clf = TorchMLPClassifier(
		hidden_layer_sizes=(1024,),
		max_iter=500,
		early_stopping=True,
		n_iter_no_change=20,
		debug=True,
	)
	clf.fit(X_train, y_train, X_val=X_val, y_val=y_val)
	score_all = clf.score(X_test, y_test)
	print(f'Done\tTime elapsed = {time.time() - start:.2f}s')

	# Extreme low-shot linear evaluation
	print('Performing linear evaluation with 5 example per class')
	start = time.time()
	score_5 = utils.eval_linear_low_shot(X_train, y_train, X_val, y_val, X_test, y_test, n=5)
	print(f'Done\tTime elapsed = {time.time() - start:.2f}s')

	results_dict = dict(
		score_all = score_all,
		score_5 = score_5,
	)

	return results_dict


def get_data(args):
	if args.dataset == 'fsd50k':
		return get_fsd50k(args)


def get_fsd50k(args):
	norm_stats = [-4.950, 5.855]
	eval_train_loader = DataLoader(
		datasets.FSD50K(args, split='train', transform=None, norm_stats=norm_stats, crop_frames=711),
		batch_size=args.batch_size, shuffle=True, num_workers=args.num_workers, pin_memory=True, drop_last=False,
	)
	eval_val_loader = DataLoader(
		datasets.FSD50K(args, split='val', transform=None, norm_stats=norm_stats, crop_frames=711),
		batch_size=args.batch_size, shuffle=True, num_workers=args.num_workers, pin_memory=True, drop_last=False,
	)
	eval_test_loader = DataLoader(
		datasets.FSD50K(args, split='test', transform=None, norm_stats=norm_stats, crop_frames=711),
		batch_size=args.batch_size, shuffle=True, num_workers=args.num_workers, pin_memory=True, drop_last=False,
	)
	return eval_train_loader, eval_val_loader, eval_test_loader


def load_model(args):
	model = ModelWrapper(args)
	model = model.encoder

	if args.model_file_path != "":
		sd = torch.load(args.model_file_path, map_location='cpu')
		if 'model' in sd.keys():
			sd = sd.get('model')
		while True:
			clean_sd = {k.replace("backbone.encoder.", ""): v for k, v in sd.items() if "backbone.encoder." in k}
			if clean_sd:
				break
			clean_sd = {k.replace("encoder.encoder.", ""): v for k, v in sd.items() if "encoder.encoder." in k}
			if clean_sd:
				break
			clean_sd = sd
			break

		model.load_state_dict(clean_sd, strict=True)
	return model



if __name__ == '__main__':

	parser = argparse.ArgumentParser(description='Linear eval', parents=hyperparameters.get_hyperparameters())
	parser.add_argument('--model_file_path', type=str, default="")
	parser.add_argument('--model_name', type=str, default="")
	parser.add_argument('--model_epoch', type=int, default=100)
	args = parser.parse_args()


	log_dir = f"logs/linear_eval/{args.dataset}/{args.model_name}/"
	os.makedirs(log_dir, exist_ok=True)
	log_path = os.path.join(log_dir, f"log.csv")
	logger = logging.getLogger()
	logger.setLevel(logging.INFO)  # Setup the root logger
	logger.addHandler(logging.FileHandler(log_path, mode="a"))

	# Get data
	eval_train_loader, eval_val_loader, eval_test_loader = get_data(args)

	# Load model
	model = load_model(args)
	model = model.cuda()
	model.eval()

	# Linear evaluation 
	scores = eval_linear(model, eval_train_loader, eval_val_loader, eval_test_loader, args.use_fp16_eval)
	score_all = scores.get('score_all')
	score_5 = scores.get('score_5')
	logger.info('epoch,{},linear_score,{},linear_score_5_mean,{},linear_score_5_std,{}'.format(
				args.model_epoch,score_all,score_5[0],score_5[1]))