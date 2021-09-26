# Created by xunannancy at 2021/9/21
import warnings
warnings.filterwarnings('ignore')
import pickle
import numpy as np
import torch
import torch.nn as nn
from utils import target_name_categories_dict, target_name_index_dict, Pytorch_DNN_exp, num_features, print_network, \
    Pytorch_DNN_validation, Pytorch_DNN_testing, merge_parameters, seqlen, TSLoader, run_evaluate
import os
from sklearn.model_selection import ParameterGrid
from collections import OrderedDict
import json
import yaml
import argparse
from copy import deepcopy

class RegressorNet(nn.Module):
    def __init__(self, hidden_size, num_layers, dropout, num_target_label):
        super().__init__()

        self.hidden_size = hidden_size

        modules = list()
        last_dims = seqlen * num_features
        for l in range(num_layers):
            modules.append(
                nn.Sequential(
                    nn.Linear(last_dims, self.hidden_size),
                    nn.LeakyReLU(),
                    nn.Dropout(p=dropout)
                )
            )
            last_dims = self.hidden_size
        modules.append(nn.Linear(last_dims, num_target_label))
        self.fcs = nn.Sequential(*modules)
        # self.final_layer = nn.Sequential(
        #     nn.Linear(last_dims, 1024),
        #     nn.LeakyReLU(),
        #     nn.Dropout(p=dropout),
        #     nn.Linear(1024, num_target_label)
        # )

    def forward(self, x):
        batch_size = x.shape[0]
        pred = self.fcs(x.reshape([batch_size, seqlen * num_features]))
        # pred = self.final_layer(output)
        return pred

    def loss_function(self, batch):
        x, y = batch
        pred = self.forward(x)
        loss = nn.CrossEntropyLoss()(pred, y)
        return loss, torch.argmax(pred, dim=-1)

class FNN_exp(Pytorch_DNN_exp):
    def __init__(self, data_path, param_dict, config):
        super().__init__(data_path, param_dict, config)

        self.dataloader = TSLoader(data_path, param_dict, config)

        self.model = self.load_model()

    def load_model(self):
        model = RegressorNet(
            hidden_size=self.param_dict['hidden_size'],
            num_layers=self.param_dict['num_layers'],
            dropout=self.param_dict['dropout'],
            num_target_label=len(self.dataloader.new_true_label_mapping)
        )
        print_network(model)
        return model

def grid_search_FNN(config):
    torch.manual_seed(config['logging_params']['manual_seed'])
    torch.cuda.manual_seed(config['logging_params']['manual_seed'])
    np.random.seed(config['logging_params']['manual_seed'])

    saved_folder = os.path.join(config['logging_params']['save_dir'], config['logging_params']['name'])
    flag = True
    while flag:
        if config['exp_params']['test_flag']:
            last_version = config['exp_params']['last_version'] - 1
        else:
            if not os.path.exists(saved_folder):
                os.makedirs(saved_folder)
                last_version = -1
            else:
                last_version = sorted([int(i.split('_')[1]) for i in os.listdir(saved_folder) if i.startswith('version_')])[-1]
        log_dir = os.path.join(saved_folder, f'version_{last_version+1}')
        if config['exp_params']['test_flag']:
            assert os.path.exists(log_dir)
            flag = False
        else:
            try:
                os.makedirs(log_dir)
                flag = False
            except:
                flag = True
    print(f'log_dir: {log_dir}')

    data_path = config['exp_params']['data_path']
    param_grid = {
        'hidden_size': config['model_params']['hidden_size'],
        'num_layers': config['model_params']['num_layers'],
        'batch_size': config['exp_params']['batch_size'],
        'learning_rate': config['exp_params']['learning_rate'],
        'dropout': config['model_params']['dropout'],
        'normalization': config['exp_params']['normalization'],
        'target_name': config['exp_params']['target_name'],
        'label_constraints': config['exp_params']['label_constraints'],
    }
    origin_param_dict_list = list(ParameterGrid(param_grid))
    param_dict_list, param_dict_nick = list(), list()
    # remove label_constraints=True/False for fault type
    for param_index, param_dict in enumerate(origin_param_dict_list):
        if param_dict['target_name'] == 'fault':
            tmp = deepcopy(param_dict)
            del tmp['label_constraints']
            cur_nick = '_'.join(map(str, (OrderedDict(tmp).values())))
            if cur_nick in param_dict_nick:
                continue
            param_dict_nick.append(cur_nick)
        param_dict_list.append(origin_param_dict_list[param_index])

    """
    getting validation results
    """
    if not config['exp_params']['test_flag']:
        Pytorch_DNN_validation(data_path, param_dict_list, log_dir, config, FNN_exp)

    """
    hyperparameters selection
    """
    summary, param_dict_res = OrderedDict(), dict()
    for target_name in config['exp_params']['target_name']:
        summary[target_name] = OrderedDict()
        for param_index, param_dict in enumerate(param_dict_list):
            if param_dict['target_name'] != target_name:
                continue
            param_dict = OrderedDict(param_dict)
            setting_name = target_name
            for key, val in param_dict.items():
                if key == 'target_name':
                    continue
                setting_name += f'_{key[0].capitalize()}{val}'
            model_list = [i for i in os.listdir(os.path.join(log_dir, setting_name, 'version_0')) if i.endswith('.ckpt')]
            assert len(model_list) == 1
            perf = float(model_list[0][model_list[0].find('avg_val_metric=')+len('avg_val_metric='):model_list[0].find('.ckpt')])
            summary[target_name]['_'.join(map(str, [j for i, j in param_dict.items() if i!='target_name']))] = perf

        reference = np.array(list(summary[target_name].values()))
        if target_name in ['fault', 'location']:
            selected_index = np.argmax(reference)
        elif target_name == 'starttime':
            selected_index = np.argmin(reference)
        selected_params = list(summary[target_name].keys())[selected_index]
        param_dict_res[target_name] = {
            'batch_size': int(selected_params.split('_')[0]),
            'dropout': float(selected_params.split('_')[1]),
            'hidden_size': int(selected_params.split('_')[2]),
            'label_constraints': eval(selected_params.split('_')[3]),
            'learning_rate': float(selected_params.split('_')[4]),
            'normalization': selected_params.split('_')[5],
            'num_layers': int(selected_params.split('_')[6]),
        }

    with open(os.path.join(log_dir, 'val_summary.json'), 'w') as f:
        json.dump(summary, f, indent=4)
    with open(os.path.join(log_dir, 'param.json'), 'w') as f:
        json.dump(param_dict_res, f, indent=4)

    """
    prediction on testing
    """
    with open(os.path.join(log_dir, 'param.json'), 'r') as f:
        param_dict = json.load(f)
    Pytorch_DNN_testing(data_path, param_dict, log_dir, config, FNN_exp)

    if not os.path.exists(os.path.join(log_dir, 'config.yaml')):
        with open(os.path.join(log_dir, 'config.yaml'), 'w') as f:
            yaml.dump(config, f)

    evaluate_config = {
        'exp_params': {
            'prediction_path': log_dir,
            'data_path': config['exp_params']['data_path'],
        },
    }
    run_evaluate(config=evaluate_config, verbose=False)
    return

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=None)
    parser.add_argument('--train_valid_ratio', '-train_valid_ratio', type=float, help='select hyperparameters on validation set')
    parser.add_argument('--manual_seed', '-manual_seed', type=int, help='manual_seed')

    # model-specific features
    parser.add_argument('--hidden_size', '-hidden_size', type=str, help='list of hidden_size')
    parser.add_argument('--num_layers', '-num_layers', type=str, help='list of num_layers')
    parser.add_argument('--batch_size', '-batch_size', type=str, help='list of batch_size')
    parser.add_argument('--max_epochs', '-max_epochs', type=int, help='number of epochs')
    parser.add_argument('--learning_rate', '-learning_rate', type=int, help='list of learning rate')
    parser.add_argument('--gpus', '-g', type=str)#, default='[1]')
    parser.add_argument('--dropout', '-dropout', type=str, help='list of dropout rates')

    parser.add_argument('--label_constraints', '-label_constraints', type=str, help='list of optional label constraints')
    parser.add_argument('--target_name', '-target_name', type=str, help='subtasks to complete')

    args = vars(parser.parse_args())
    with open('./../configs/FNN.yaml', 'r') as file:
        try:
            config = yaml.safe_load(file)
        except yaml.YAMLError as exc:
            print(exc)
    config = merge_parameters(args, config)
    print(f'after merge: config, {config}')


    print('gpus: ', config['trainer_params']['gpus'])
    if np.sum(config['trainer_params']['gpus']) < 0:
        config['trainer_params']['gpus'] = 0

    grid_search_FNN(config)

    """
    std:
    constrained
    summary: {'#samples': 110, 'fault': 0.6021423589198526, 'location': -1, 'starttime': -1}    
    summary: {'#samples': 110, 'fault': -1, 'location': 0.21080139372822299, 'starttime': -1}    
    summary: {'#samples': 110, 'fault': -1, 'location': -1, 'starttime': 42.47764227642277}
    non-constrained
    summary: {'#samples': 110, 'fault': -1, 'location': 0.18418505613627564, 'starttime': -1}
    summary: {'#samples': 110, 'fault': -1, 'location': -1, 'starttime': 52.73170731707317}
    """














