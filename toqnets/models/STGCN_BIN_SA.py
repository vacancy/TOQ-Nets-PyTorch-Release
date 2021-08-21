#! /usr/bin/env python3
# -*- coding: utf-8 -*-
# File   : STGCN_BIN_SA.py
# Author : Zhezheng Luo
# Email  : luozhezheng@gmail.com
# Date   : 08/02/2021
#
# This file is part of TOQ-Nets-PyTorch.
# Distributed under terms of the MIT license.

from copy import deepcopy

import torch
from torch import nn

from toqnets.config_update import update_config, ConfigUpdate
from toqnets.nn.input_transform.input_transform import InputTransformPredefined
from toqnets.nn.stgcn import STGCN
from toqnets.nn.utils import apply_last_dim, move_player_first, init_weights


class STGCN_BIN_SA(nn.Module):
    default_config = {
        'name': 'STGCN_BIN_SA',
        'n_agents': 13,
        'state_dim': 3,
        'image_dim': None,
        'type_dim': 4,
        'n_actions': 9,
        'h_dim': 128,
        'n_features': 256,
        'kernel_size': (7, 7),  # (temporal_kernel_size, spatial_kernel_size)
        'edge_importance_weighting': True,
        'noise': None,
        'input_feature': 'physical',
        'cmp_dim': 5,
        'max_gcn': False,
    }

    @classmethod
    def complete_config(cls, config_update, default_config=None):
        assert isinstance(config_update, ConfigUpdate)
        config = deepcopy(cls.default_config) if default_config is None else default_config
        update_config(config, config_update)
        for k in cls.default_config:
            if k not in config:
                config[k] = deepcopy(cls.default_config[k])
        return config

    def __init__(self, config):
        super().__init__()
        self.config = config

        n_agents = self.config['n_agents']
        state_dim = self.config['state_dim']
        type_dim = self.config['type_dim']
        n_actions = self.config['n_actions']
        n_features = self.config['n_features']
        kernel_size = self.config['kernel_size']
        edge_importance_weighting = self.config['edge_importance_weighting']
        input_feature = self.config['input_feature']
        cmp_dim = self.config['cmp_dim']
        binary_input_dim, binary_output_dim = 0, 0
        if input_feature == 'physical':
            self.input_transform = InputTransformPredefined(
                type_dim=type_dim, cmp_dim=cmp_dim,
                time_reduction='none'
            )
            binary_input_dim = self.input_transform.out_dims[2]
            binary_output_dim = binary_input_dim
        elif input_feature is None:
            add_state_dim = 0
        else:
            raise ValueError()

        self.state_encoder = lambda x: x  # AgentEncoder(state_dim + type_dim, h_dim, h_dim)

        self.stgcn = STGCN(n_agents, state_dim + type_dim, n_features, kernel_size,
                           edge_importance_weighting, binary_input_dim=binary_input_dim,
                           binary_output_dim=binary_output_dim, max_gcn=self.config['max_gcn'])

        self.decoder = nn.Linear(n_features, n_actions)

    def forward(self, data=None, hp=None):
        """
        :param data:
            'trajectories': [batch, length, n_agents, state_dim]
            'playerid': [batch]
            * 'images': [batch, length, n_agents, W, H, C]
            'actions': [batch]
            'types': [batch, n_agents, type_dim]
        :param hp: hyper parameters
        """
        types = data['types'].type(torch.float)
        trajectories = data['trajectories']
        playerid = data['playerid'] if 'playerid' in data else None
        actions = data['actions']
        device = trajectories.device
        batch, length, n_agents, state_dim = trajectories.size()
        assert n_agents == self.config['n_agents']
        assert state_dim == self.config['state_dim']
        assert actions.size() == torch.Size((batch,))
        beta = hp['beta']

        if self.config['noise'] is not None:
            noise_std = torch.zeros_like(trajectories)
            noise_std[:, :, :, :2] = self.config['noise']
            trajectories = torch.normal(trajectories, noise_std)

        if playerid is not None and playerid.max() > -0.5:
            states = move_player_first(trajectories, playerid)
            types = torch.zeros(states.size(0), states.size(2), 4, device=states.device)
            n_players = n_agents // 2
            for k in range(n_agents):
                tp = (0 if k == 0 else 1) if k <= 1 else (2 if k <= n_players else 3)
                types[:, k, tp] = 1
        else:
            states = trajectories
        if 'estimate_parameters' in hp and hp['estimate_parameters']:
            self.input_transform.estimate_parameters(states)
            return None

        if self.config['input_feature'] == 'physical':
            nlm_inputs = self.input_transform(states, add_unary_tensor=types, beta=beta)
            binary_input = nlm_inputs[2]
        elif self.config['input_feature'] is None:
            pass
        else:
            raise ValueError()

        inputs = torch.cat([states, types.unsqueeze(1).repeat(1, length, 1, 1)], dim=3)

        x = self.stgcn(inputs, binary_input)

        n_features = x.size(1)
        x = x.mean(dim=(2, 3))

        assert x.size() == torch.Size((batch, n_features))

        x = apply_last_dim(self.decoder, x.contiguous())
        return {'output': x,
                'target': actions,
                'loss': torch.zeros(1, device=trajectories.device)}

    def reset_parameters(self):
        init_weights(self.stgcn.st_gcn_layers[0])

    def set_grad(self, option):
        if option == 'all':
            for param in self.parameters():
                param.requires_grad_(True)
        elif option == 'none':
            for param in self.parameters():
                param.requires_grad_(False)
        elif option == 'gfootball_finetune':
            for param in self.parameters():
                param.requires_grad_(False)
            for param in self.stgcn.st_gcn_layers[0].parameters():
                param.requires_grad_(True)
        else:
            raise ValueError()
