# Copyright (c) 2023 Contextual AI, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
"""
Extendable Trainer classes for aligning LLMs.
The specific class that should be used should be specified in the loss file under config/loss.

The BasicTrainer contains the core methods (e.g., sharding, basic training loop, etc.).
The SFTTrainer, PairedPreferenceTrainer, and UnpairedPreferenceTrainer all subclass BasicTrainer
and override the get_batch_metrics() and (optionally) forward() methods.

The trainer for each loss should subclass either PairedPreferenceTrainer or UnpairedPreferenceTrainer.
"""

import torch
torch.backends.cuda.matmul.allow_tf32 = True
import torch.nn.functional as F
import torch.nn as nn
import transformers
import gc
from omegaconf import OmegaConf, DictConfig
from transformers import AutoTokenizer

import torch.distributed as dist
from torch.distributed.fsdp import (
    FullyShardedDataParallel as FSDP,
    MixedPrecision,
    StateDictType,
    BackwardPrefetch,
    ShardingStrategy,
    CPUOffload,
)
from torch.distributed.fsdp.api import FullStateDictConfig, FullOptimStateDictConfig
from torch.distributed.fsdp.wrap import transformer_auto_wrap_policy, size_based_auto_wrap_policy
import tensor_parallel as tp
import contextlib

import dataloader
from utils import (
    slice_and_move_batch_for_device,
    formatted_dict,
    all_gather_if_needed,
    pad_to_length,
    get_block_class_from_model,
    rank0_print,
    get_batch_logps,
    masked_mean,
    masked_var,
    entropy_from_logits,
    delete_dict,
    rowwise_product,
)
import numpy as np
import wandb
import tqdm

import random
import os
from collections import defaultdict
import time
import json
import functools
from typing import Optional, Dict, List, Union, Tuple


class BasicTrainer(object):
    def __init__(self, 
                 tokenizer: AutoTokenizer, 
                 config: DictConfig, 
                 train_iterator: dataloader.DataLoader, 
                 eval_iterator: dataloader.DataLoader, 
                 policy: nn.Module, 
                 reference_model: Optional[nn.Module] = None, 
                 rank: int = 0, 
                 world_size: int = 1, 
                 fsdp: bool = False,
                 ):
        """A trainer for a language model, supporting either SFT, HALO, or offline PPO training.
        """
        self.seed = config.seed
        torch.manual_seed(self.seed)
        np.random.seed(self.seed)
        random.seed(self.seed)

        self.rank = rank
        self.device = torch.device('cuda', self.rank)
        self.world_size = world_size
        self.config = config
        self.run_dir = config.local_run_dir
        self.fsdp = fsdp

        self.tokenizer = tokenizer
        self.policy = policy
        self.policy_dtype = getattr(torch, config.model.policy_dtype)
        self.reference_model = reference_model
        self.example_counter = 0
        self.batch_counter = 0

        self.train_iterator = train_iterator
        self.eval_iterator = eval_iterator
        self.eval_batches = list(self.eval_iterator)
        rank0_print(f'Loaded {len(self.eval_batches)} eval batches of size {config.model.eval_batch_size}')

        if self.fsdp:
            self.shard()

        self.is_mistral = 'mistral' in self.config.model.name_or_path.lower()
        
    def shard(self):
        """
        Shard the policy model and reference model (if applicable) using FDSP.
        """
        assert self.config.model.block_name is not None, 'must specify model.block_name (e.g., GPT2Block or GPTNeoXLayer) for FSDP'
        wrap_class = get_block_class_from_model(self.policy.pretrained_model if self.config.loss.name == 'ppo' else self.policy, self.config.model.block_name)
        model_auto_wrap_policy = functools.partial(transformer_auto_wrap_policy, transformer_layer_cls={wrap_class},)

        shared_fsdp_kwargs = dict(
            auto_wrap_policy=model_auto_wrap_policy,
            sharding_strategy=ShardingStrategy.FULL_SHARD,
            cpu_offload=CPUOffload(offload_params=False),
            backward_prefetch=BackwardPrefetch.BACKWARD_PRE,
            device_id=self.rank,
            ignored_modules=None,
            limit_all_gathers=False,
            use_orig_params=False,
            sync_module_states=False
        )

        rank0_print('Sharding models...')
        mp_dtype = getattr(torch, self.config.model.fsdp_policy_mp) if self.config.model.fsdp_policy_mp is not None else None
        policy_mp_policy = MixedPrecision(param_dtype=mp_dtype, reduce_dtype=mp_dtype, buffer_dtype=mp_dtype)

        if self.config.loss.name == 'ppo':
            self.policy.pretrained_model = FSDP(self.policy.pretrained_model, **shared_fsdp_kwargs, mixed_precision=policy_mp_policy)

            # shard the value head according to size
            v_head_shared_fsdp_kwargs = dict(
                auto_wrap_policy=functools.partial(size_based_auto_wrap_policy, min_num_params=100),
                sharding_strategy=ShardingStrategy.FULL_SHARD,
                cpu_offload=CPUOffload(offload_params=False),
                backward_prefetch=BackwardPrefetch.BACKWARD_PRE,
                device_id=self.rank,
                ignored_modules=None,
                limit_all_gathers=False,
                use_orig_params=False,
                sync_module_states=False
            )
            self.policy.v_head = FSDP(self.policy.v_head, **v_head_shared_fsdp_kwargs)
        else:
            self.policy = FSDP(self.policy, **shared_fsdp_kwargs, mixed_precision=policy_mp_policy)

        if self.reference_model is not None:
            self.reference_model = FSDP(self.reference_model, **shared_fsdp_kwargs, mixed_precision=policy_mp_policy)

        if self.config.model.activation_checkpointing:
            rank0_print('Attempting to enable activation checkpointing...')
            try:
                # use activation checkpointing, according to:
                # https://pytorch.org/blog/scaling-multimodal-foundation-models-in-torchmultimodal-with-pytorch-distributed/
                # first, verify we have FSDP activation support ready by importing:
                from torch.distributed.algorithms._checkpoint.checkpoint_wrapper import (
                    checkpoint_wrapper,
                    apply_activation_checkpointing,
                    CheckpointImpl,
                )
            except Exception as e:
                rank0_print('FSDP activation checkpointing not available:', e)
            else:
                check_fn = lambda submodule: isinstance(submodule, wrap_class)
                rank0_print('Applying activation checkpointing wrapper to policy...')

                if self.config.loss.name == 'ppo':
                    apply_activation_checkpointing(self.policy.pretrained_model, checkpoint_wrapper_fn=checkpoint_wrapper, check_fn=check_fn)
                else:
                    apply_activation_checkpointing(self.policy, checkpoint_wrapper_fn=checkpoint_wrapper, check_fn=check_fn)

                if self.reference_model is not None:
                    apply_activation_checkpointing(self.reference_model, checkpoint_wrapper_fn=checkpoint_wrapper, check_fn=check_fn)

                rank0_print('FSDP activation checkpointing enabled!')

        print('Loaded model on rank', self.rank)
        dist.barrier()
            
    def get_batch_samples(self, batch: Dict[str, torch.LongTensor]) -> Tuple[str, str]:
        """Generate samples from the policy."""
        ctx = lambda: (FSDP.summon_full_params(self.policy, writeback=False, recurse=False) if self.fsdp else contextlib.nullcontext())
        with ctx():
            policy_output = self.policy.generate(
                batch['prompt_input_ids'],
                attention_mask=batch['prompt_attention_mask'],
                max_length=self.config.model.max_length,
                do_sample=True,
                pad_token_id=self.tokenizer.pad_token_id,
                top_p=self.config.top_p,
            )
        
            policy_output = pad_to_length(policy_output, self.config.model.max_length, self.tokenizer.pad_token_id)
            policy_output = all_gather_if_needed(policy_output, self.rank, self.world_size)
            policy_output_decoded = self.tokenizer.batch_decode(policy_output, skip_special_tokens=True)

        return policy_output_decoded

    def loss(self,
             policy_chosen_logps: torch.FloatTensor,
             policy_rejected_logps: torch.FloatTensor,
             reference_chosen_logps: torch.FloatTensor,
             reference_rejected_logps: torch.FloatTensor) -> Tuple[torch.FloatTensor, torch.FloatTensor, torch.FloatTensor]:
        """
        Args:
            policy_chosen_logps: Log probabilities of the policy model for the chosen responses. Shape: (batch_size,)
            policy_rejected_logps: Log probabilities of the policy model for the rejected responses. Shape: (batch_size,)
            reference_chosen_logps: Log probabilities of the reference model for the chosen responses. Shape: (batch_size,)
            reference_rejected_logps: Log probabilities of the reference model for the rejected responses. Shape: (batch_size,)

        Returns:
            A tuple of three tensors: (losses, chosen_rewards, rejected_rewards).
            The losses tensor contains the losses, one for each example (sif chosen_only or rejected_only, only n/2 losses).
            The chosen_rewards and rejected_rewards tensors contain the rewards for the chosen and rejected responses, respectively, for reporting.
            Note that rejected responses do not factor into the loss, only the reward calculation.
        """
        raise NotImplementedError

    def get_batch_metrics(self, batch: Dict[str, Union[List, torch.LongTensor]], mode: str=None) -> Tuple[torch.FloatTensor, Dict]:
        """Compute the loss and other metrics for the given batch of inputs.
        
        Arg:
            batch: dictionary of inputs for the batch (what is required will vary depending on the trainer)
            mode: one of 'train', 'eval', 'sample'
        """
        raise NotImplementedError

    def eval(self) -> Dict[str, Dict]:
        """
        Run evaluation on all the examples in the test data and return the metrics from get_batch_metrics.
        This is close-ended evaluation and measures the performance of a single model on a single dataset. 
        It does not compare two models to eacch other.

        Returns:
            A dict of form:
            {
                'metadata': the Hydra config
                'results': a dict of batch metrics (averaged across all of the test data)
            }
        """
        rank0_print(f'Running evaluation')
        self.policy.eval()

        if self.reference_model is not None:
            self.reference_model.eval()

        all_eval_metrics = defaultdict(list)
        
        for eval_batch in (tqdm.tqdm(self.eval_batches, desc='Computing eval metrics') if self.rank == 0 else self.eval_batches):
            local_eval_batch = slice_and_move_batch_for_device(eval_batch, self.rank, self.world_size, self.rank)
            with torch.no_grad():
                _, eval_metrics = self.get_batch_metrics(local_eval_batch, mode='eval')

            for k, v in eval_metrics.items():
                all_eval_metrics[k].extend(v)

        mean_eval_metrics = {}
        for k, v in all_eval_metrics.items():
            if len(v) > 0:
                mean_eval_metrics[k] = sum(v) / len(v)

        results = {
            'metadata': OmegaConf.to_object(self.config),
            'results': formatted_dict(mean_eval_metrics),
        }
        return results

    def sample(self, include_original_prompt=False) -> List[Dict[str, str]]:
        """
        Generate samples from the policy model.
        
        Args:
            include_original_prompt: whether to include the original prompt among the generated text

        Returns:
            A list of samples, each of which is of the form:
            {
                'prompt': the input
                'chosen': the generation chosen by the human for the given prompt
                'policy': the generation from the policy model
            }
        """
        all_policy_samples, all_prompts, all_chosen, all_original_prompts = [], [], [], []
        samples = []

        self.policy.eval()
        if self.reference_model is not None:
            self.reference_model.eval()

        for eval_batch in self.eval_batches:
            local_eval_batch = slice_and_move_batch_for_device(eval_batch, self.rank, self.world_size, self.rank)
            policy_samples = self.get_batch_samples(local_eval_batch)

            chosen_samples = []
            # for DPO-like losses, chosen_text is the field that will contain the text; target_text for all other losses
            # be sure to remove EOS token if present
            for x in (eval_batch['target_text'] if 'target_text' in eval_batch else eval_batch['chosen_text']):
                if self.tokenizer.eos_token in x:
                    x = x[:x.rfind(self.tokenizer.eos_token)]
                
                chosen_samples.append(x)

            all_prompts.extend(eval_batch['prompt_text'])
            all_original_prompts.extend(eval_batch['original_prompt'])
            all_chosen.extend(chosen_samples)
            all_policy_samples.extend(policy_samples)

            if self.config.n_samples is not None and len(all_prompts) > self.config.n_samples:
                break
            else:
                rank0_print(f"Generated {len(all_prompts)} samples ...")

        for i in range(len(all_prompts)):
            if include_original_prompt:
                samples.append({
                    'prompt' : all_prompts[i],
                    'chosen' : all_chosen[i],
                    'policy' : all_policy_samples[i][len(all_prompts[i]):], # remove the prompt
                    'original_prompt' : all_original_prompts[i],
                })
            else:
                samples.append({
                    'prompt' : all_prompts[i],
                    'chosen' : all_chosen[i],
                    'policy' : all_policy_samples[i][len(all_prompts[i]):], # remove the prompt
                })

        return samples

    def train(self):
        """Begin either SFT or HALO training, with periodic evaluation. This is subclassed when implementing PPO."""

        rank0_print(f'Using {self.config.optimizer} optimizer with learning rate {self.config.lr}')
        self.optimizer = getattr(torch.optim, self.config.optimizer)(self.policy.parameters(), lr=self.config.lr)
        self.scheduler = torch.optim.lr_scheduler.LambdaLR(self.optimizer, lr_lambda=lambda step: min(1.0, (step + 1) / (self.config.warmup_steps + 1)))

        if self.reference_model is not None:
            self.reference_model.eval()

        last_log = None
        gradients_accumulated = 0
        batch_metrics = defaultdict(list)

        for batch in self.train_iterator:
            # EVALUATION
            if self.example_counter % self.config.eval_every == 0 and (self.example_counter > 0 or self.config.do_first_eval):
                rank0_print(f'Running evaluation after {self.example_counter} train examples')
                self.policy.eval()

                all_eval_metrics = defaultdict(list)
            
                for eval_batch in (tqdm.tqdm(self.eval_batches, desc='Computing eval metrics') if self.rank == 0 else self.eval_batches):
                    local_eval_batch = slice_and_move_batch_for_device(eval_batch, self.rank, self.world_size, self.rank)
                    with torch.no_grad():
                        _, eval_metrics = self.get_batch_metrics(local_eval_batch, mode='eval')

                    for k, v in eval_metrics.items():
                        all_eval_metrics[k].extend(v)

                    delete_dict(local_eval_batch)

                mean_eval_metrics = {}
                for k, v in all_eval_metrics.items():
                    if len(v) > 0:
                        mean_eval_metrics[k] = sum(v) / len(v)
                rank0_print(f'eval after {self.example_counter}: {formatted_dict(mean_eval_metrics)}')
               
                if self.config.wandb.enabled and self.rank == 0:
                    wandb.log(mean_eval_metrics, step=self.example_counter)

                if self.example_counter > 0:
                    if self.config.debug:
                        rank0_print('skipping save in debug mode')
                    elif self.config.intermediate_checkpoints:
                        output_dir = os.path.join(self.run_dir, f'step-{self.example_counter}')
                        rank0_print(f'creating checkpoint to write to {output_dir}...')
                        self.save(output_dir, mean_eval_metrics)

                delete_dict(all_eval_metrics)
                delete_dict(mean_eval_metrics)

            #### TRAINING
            self.policy.train()

            start_time = time.time()
            
            local_microbatch = slice_and_move_batch_for_device(batch, self.rank, self.world_size, self.rank)
            loss, metrics = self.get_batch_metrics(local_microbatch)
            (loss / self.config.model.gradient_accumulation_steps).backward()

            for k, v in metrics.items():
                batch_metrics[k].extend(v)

            gradients_accumulated += 1
            
            if gradients_accumulated == self.config.model.gradient_accumulation_steps:
                grad_norm = self.clip_gradient()
                batch_metrics['grad_norm'].append(grad_norm)

                self.optimizer.step()
                self.optimizer.zero_grad()
                self.scheduler.step()
                gradients_accumulated = 0

            step_time = time.time() - start_time
            examples_per_second = self.config.model.batch_size / step_time
            batch_metrics['examples_per_second'].append(examples_per_second)
            
            self.batch_counter += 1
            self.example_counter += self.config.model.batch_size

            delete_dict(local_microbatch)
            delete_dict(metrics)

            if gradients_accumulated == 0 and (last_log is None or time.time() - last_log > self.config.minimum_log_interval_secs):
                mean_train_metrics = {}
                for k, v in batch_metrics.items():
                    if len(v) > 0:
                        mean_train_metrics[k] = sum(v) / len(v)

                mean_train_metrics['counters/examples'] = self.example_counter
                mean_train_metrics['counters/updates'] = self.batch_counter
                rank0_print(f'train stats after {self.example_counter} examples: {formatted_dict(mean_train_metrics)}')

                if self.config.wandb.enabled and self.rank == 0:
                    wandb.log(mean_train_metrics, step=self.example_counter)

                last_log = time.time()

                delete_dict(batch_metrics)
                delete_dict(mean_train_metrics)
                delete_dict(batch)
                batch_metrics = defaultdict(list)

                # explicitly empty cache if less than 100MB available
                r = torch.cuda.memory_reserved(self.rank)
                a = torch.cuda.memory_allocated(self.rank)

                if (r - a) / 1024 < 100:
                    gc.collect()
                    torch.cuda.empty_cache()
            else:
                rank0_print(f'skipping logging after {self.example_counter} examples to avoid logging too frequently')

    def clip_gradient(self):
        """Clip the gradient norm of the parameters of a non-FSDP policy."""
        return torch.nn.utils.clip_grad_norm_(self.policy.parameters(), self.config.model.max_grad_norm).item()

    def write_state_dict(self, step: int, state: Dict[str, torch.Tensor], metrics: Dict, filename: str, dir_name: Optional[str] = None):
        """Write a checkpoint to disk.
        
        Args:
            step : current training step
            state: current state of training (model or optimizer, if applicable)
            metrics: dictionary of metrics to save
            dir_name: directory in which to save
        """
        if dir_name is None:
            dir_name = os.path.join(self.run_dir, f'LATEST')

        os.makedirs(dir_name, exist_ok=True)
        output_path = os.path.join(dir_name, filename)
        rank0_print(f'writing checkpoint to {output_path}...')
        torch.save({
            'step_idx': step,
            'state': state,
            'metrics': metrics if metrics is not None else {},
        }, output_path)
    
    def save(self, output_dir: Optional[str] = None, metrics: Optional[Dict] = None, save_model_only: bool=True):
        """
        Save tokenizer, policy model, optimizer, scheduler state to disk, gathering from all processes 
        and saving only on the rank 0 process.
        """
        if self.fsdp:
            save_policy = FullStateDictConfig(offload_to_cpu=True, rank0_only=True)
            with FSDP.state_dict_type(self.policy, StateDictType.FULL_STATE_DICT, state_dict_config=save_policy):
                policy_state_dict = self.policy.state_dict()

            if self.rank == 0:
                self.write_state_dict(self.example_counter, policy_state_dict, metrics, 'policy.pt', output_dir)
                self.tokenizer.save_pretrained(self.run_dir) # save tokenizer in HF format

            del policy_state_dict
            dist.barrier()

            if not save_model_only:
                save_policy = FullOptimStateDictConfig(offload_to_cpu=True, rank0_only=True)
                with FSDP.state_dict_type(self.policy, StateDictType.FULL_STATE_DICT, optim_state_dict_config=save_policy):
                    optimizer_state_dict = FSDP.optim_state_dict(self.policy, self.optimizer)

                if self.rank == 0:
                    self.write_state_dict(self.example_counter, optimizer_state_dict, metrics, 'optimizer.pt', output_dir)
                del optimizer_state_dict
                dist.barrier()

                if self.rank == 0:
                    scheduler_state_dict = self.scheduler.state_dict()
                    self.write_state_dict(self.example_counter, scheduler_state_dict, metrics, 'scheduler.pt', output_dir)
                del scheduler_state_dict
                dist.barrier()
        else:
            self.tokenizer.save_pretrained(self.run_dir) # save tokenizer in HF format
            policy_state_dict = self.policy.state_dict()
            self.write_state_dict(self.example_counter, policy_state_dict, metrics, 'policy.pt', output_dir)
            del policy_state_dict

            if not save_model_only:
                optimizer_state_dict = self.optimizer.state_dict()
                self.write_state_dict(self.example_counter, optimizer_state_dict, metrics, 'optimizer.pt', output_dir)
                del optimizer_state_dict

                scheduler_state_dict = self.scheduler.state_dict()
                self.write_state_dict(self.example_counter, scheduler_state_dict, metrics, 'scheduler.pt', output_dir)
                del scheduler_state_dict
        

class SFTTrainer(BasicTrainer):
    def get_batch_metrics(self, batch: Dict[str, Union[List, torch.LongTensor]], mode: str=None):
        """Compute the loss and other metrics for the given batch of inputs.
        
        Args:
            batch: dictionary of inputs for the batch (should contain 'target_attention_mask', 'target_input_input_ids', 
                'target_labels' where 'target' corresponds to the SFT example)
            mode: one of 'train', 'eval', 'sample'
        """
        metrics = {}
        if mode is None: mode = self.config.mode
        
        policy_chosen_logits = self.policy(batch['target_combined_input_ids'], attention_mask=batch['target_combined_attention_mask'], use_cache=(not self.is_mistral)).logits.to(self.policy_dtype)
        policy_chosen_logps = get_batch_logps(policy_chosen_logits, batch['target_labels'], average_log_prob=False)
        losses = -policy_chosen_logps

        policy_chosen_logps = all_gather_if_needed(policy_chosen_logps.detach(), self.rank, self.world_size)
        all_devices_losses = all_gather_if_needed(losses.detach(), self.rank, self.world_size)

        metrics[f'logps_{mode}/chosen'] = policy_chosen_logps.float().cpu().numpy().tolist()
        metrics[f'loss/{mode}'] = all_devices_losses.float().cpu().numpy().tolist()

        return losses.mean(), metrics


class UnpairedPreferenceTrainer(BasicTrainer):
    """A trainer for any loss that doesn't use paired preference, like KTO."""
    def forward(self, model: nn.Module, batch: Dict[str, Union[List, torch.LongTensor]]) -> Tuple[torch.FloatTensor, torch.FloatTensor, torch.FloatTensor, torch.BoolTensor]:
        """Run the given model on the given batch of inputs.
        
        Returns:
            chosen_logps: log probabilities of chosen examples (should be batch size / 2 if data was read in correctly)
            rejected_logps: log probabilities of rejected examples (should be batch size / 2 if data was read in correctly)
        """
        all_logits = model(batch['target_combined_input_ids'], attention_mask=batch['target_combined_attention_mask'], use_cache=(not self.is_mistral)).logits.to(self.policy_dtype)
        all_logps = get_batch_logps(all_logits, batch['target_labels'], average_log_prob=False)

        assert all_logps.shape[0] == len(batch['status'])
        chosen_idx = [i for i in range(all_logps.shape[0]) if batch['status'][i] == 'chosen']
        rejected_idx = [i for i in range(all_logps.shape[0]) if batch['status'][i] == 'rejected']

        chosen_logps = all_logps[chosen_idx, ...]
        rejected_logps = all_logps[rejected_idx, ...]
        return chosen_logps, rejected_logps

    def get_batch_metrics(self, batch: Dict[str, Union[List, torch.LongTensor]], mode: str=None):
        """Compute the loss and other metrics for the given batch of inputs."""
        metrics = {}
        if mode is None: mode = self.config.mode

        if self.reference_model is None:
            policy_chosen_logps, policy_rejected_logps = self.forward(self.policy, batch)
            losses, chosen_rewards, rejected_rewards = self.loss(policy_chosen_logps, policy_rejected_logps)
        else:
            policy_chosen_logps, policy_rejected_logps = self.forward(self.policy, batch)
            with torch.no_grad():
                reference_chosen_logps, reference_rejected_logps = self.forward(self.reference_model, batch)
            losses, chosen_rewards, rejected_rewards = self.loss(policy_chosen_logps, policy_rejected_logps, reference_chosen_logps, reference_rejected_logps)

        # all_gather treats empty lists/tensors poorly, and empty lists can occur because a batch can contain all chosen or all rejected example
        # therefore, concatenate chosen + rejected rewards before all_gather
        combined_rewards = torch.cat((chosen_rewards.detach(), rejected_rewards.detach()), 0)
        combined_statuses = torch.Tensor([1] * len(chosen_rewards) + [0] * len(rejected_rewards)).to(self.device)

        all_rewards = all_gather_if_needed(combined_rewards, self.rank, self.world_size)
        all_statuses = all_gather_if_needed(combined_statuses, self.rank, self.world_size)
        chosen_rewards_idx = [ i for i in range(len(all_statuses)) if all_statuses[i].item() == 1 ]
        rejected_rewards_idx = [ i for i in range(len(all_statuses)) if all_statuses[i].item() == 0 ]
        all_devices_losses = all_gather_if_needed(losses.detach(), self.rank, self.world_size)

        metrics[f'rewards_{mode}/chosen'] = all_rewards[chosen_rewards_idx].float().cpu().numpy().tolist()
        metrics[f'rewards_{mode}/rejected'] = all_rewards[rejected_rewards_idx].float().cpu().numpy().tolist()
        metrics[f'rewards_{mode}/margins'] = [(all_rewards[chosen_rewards_idx].mean().nan_to_num(0) - all_rewards[rejected_rewards_idx].mean().nan_to_num(0)).item()]
        metrics[f'loss/{mode}'] = all_devices_losses.float().cpu().numpy().tolist()

        del policy_chosen_logps, policy_rejected_logps
        del combined_rewards, combined_statuses, all_rewards, all_statuses, chosen_rewards_idx, rejected_rewards_idx, all_devices_losses

        if self.reference_model:
            del reference_chosen_logps, reference_rejected_logps

        return losses.mean(), metrics


class SimpleKTOTrainer(UnpairedPreferenceTrainer):
    def loss(self,
             policy_chosen_logps: torch.FloatTensor,
             policy_rejected_logps: torch.FloatTensor,
             reference_chosen_logps: torch.FloatTensor,
             reference_rejected_logps: torch.FloatTensor) -> Tuple[torch.FloatTensor, torch.FloatTensor, torch.FloatTensor]:
        """Compute the Kahneman-Tversky loss for a batch of policy and reference model log probabilities. 
        For each batch of n/2 chosen examples and n/2 rejected examples (belonging to n different inputs), calculate the loss as follows.

        If generation y ~ p_chosen, where x' ~ are the examples with rejected generations, we have the 'chosen' loss:
            L(x, y) := 1 - sigmoid(beta * ([log p_policy(y|x) - log p_reference(y|x)] - KL(p_policy(y_rejected|x') || p_reference(y_rejected|x')))
        If generation y ~ p_rejected, , where x' ~ are the examples with chosen generations, we have the 'rejected' loss:
            L(x, y) := 1 - sigmoid(beta * (KL(p_policy(y_chosen|x') || p_reference(y_chosen|x')) - [log p_policy(y|x) - log p_reference(y|x)]))
        """
        chosen_KL = (policy_chosen_logps - reference_chosen_logps).mean().clamp(min=0)
        rejected_KL = (policy_rejected_logps - reference_rejected_logps).mean().clamp(min=0)

        chosen_logratios = (policy_chosen_logps - reference_chosen_logps)
        rejected_logratios = (policy_rejected_logps - reference_rejected_logps)

        losses = torch.cat((1 - F.sigmoid(self.config.loss.beta * (chosen_logratios - rejected_KL)), 1 - F.sigmoid(self.config.loss.beta * (chosen_KL - rejected_logratios))), 0)

        chosen_rewards = self.config.loss.beta * (policy_chosen_logps - reference_chosen_logps).detach()
        rejected_rewards = self.config.loss.beta * (policy_rejected_logps - reference_rejected_logps).detach()

        return losses, chosen_rewards, rejected_rewards


class KTOTrainer(UnpairedPreferenceTrainer):
    def loss(self,
             policy_chosen_logps: torch.FloatTensor,
             policy_rejected_logps: torch.FloatTensor,
             policy_KL_logps: torch.FloatTensor,
             reference_chosen_logps: torch.FloatTensor,
             reference_rejected_logps: torch.FloatTensor,
             reference_KL_logps) -> Tuple[torch.FloatTensor, torch.FloatTensor, torch.FloatTensor]:
        """Compute the Kahneman-Tversky loss for a batch of policy and reference model log probabilities.

        If generation y ~ p_desirable, we have the 'desirable' loss:
            L(x, y) := 1 - sigmoid(beta * ([log p_policy(y|x) - log p_reference(y|x)] - KL(p_policy || p_reference)))
        If generation y ~ p_undesirable, we have the 'undesirable' loss:
            L(x, y) := 1 - sigmoid(beta * (KL(p_policy || p_reference) - [log p_policy(y|x) - log p_reference(y|x)]))

        The desirable losses are weighed by config.loss.desirable_weight.
        The undesirable losses are weighed by config.loss.undesirable_weight.
        This should be used to address imbalances in the ratio of desirable:undesirable examples respectively.

        The KL term is estimated by matching x with unrelated outputs y', then calculating the average log ratio
        log p_policy(y'|x) - log p_reference(y'|x). Doing so avoids the requirement that there be equal numbers of 
        desirable and undesirable examples in the microbatch.
        """
        KL = (policy_KL_logps - reference_KL_logps).mean().detach()
        # nn.all_reduce sums up the KL estimates across all devices (gradient will also be scaled by world size)
        dist.nn.all_reduce(KL, op=dist.ReduceOp.SUM)
        # take average (will also scale gradients appropriately)
        KL = (KL / self.world_size).clamp(min=0)

        if policy_chosen_logps.shape[0] != 0:
            chosen_logratios = (policy_chosen_logps - reference_chosen_logps)
            chosen_losses = 1 - F.sigmoid(self.config.loss.beta * (chosen_logratios - KL))
            chosen_rewards = self.config.loss.beta * chosen_logratios.detach()
        else:
            # important to cast to policy_dtype; otherwise error will occur during all_gather
            chosen_losses = torch.Tensor([]).to(self.policy_dtype).to(self.device)
            chosen_rewards = torch.Tensor([]).to(self.policy_dtype).to(self.device)
        
        if policy_rejected_logps.shape[0] != 0:
            rejected_logratios = (policy_rejected_logps - reference_rejected_logps)
            rejected_losses = 1 - F.sigmoid(self.config.loss.beta * (KL - rejected_logratios))
            rejected_rewards = self.config.loss.beta * rejected_logratios.detach()
        else:
            # important to cast to policy_dtype; otherwise error will occur during all_gather
            rejected_losses = torch.Tensor([]).to(self.policy_dtype).to(self.device)
            rejected_rewards = torch.Tensor([]).to(self.policy_dtype).to(self.device)

        losses = torch.cat((self.config.loss.desirable_weight * chosen_losses, self.config.loss.undesirable_weight * rejected_losses), 0)

        return losses, chosen_rewards, rejected_rewards, KL
    
    def forward(self, model: nn.Module, batch: Dict[str, Union[List, torch.LongTensor]]) -> Tuple[torch.FloatTensor, torch.FloatTensor, torch.FloatTensor]:
        """Run the given model on the given batch of inputs. The examples used to calculate the rewards and the KL term should be
        processed in a single forward pass, since the gradient is taken wrt both groups. Doing it in multiple forward passes will give
        you a RuntimeError: 'The tensor has a non-zero number of elements, but its data is not allocated yet.'
        
        Args:
            - model: the model to use for the forward pass
            - batch: the microbatch (should have the input ids, attention mask, and labels)

        Returns:
            chosen_logps: log probabilities of chosen examples (should be batch size / 2 if data was read in correctly)
            rejected_logps: log probabilities of rejected examples (should be batch size / 2 if data was read in correctly)
            KL_logps: log probabilities of the unmatched y'|x (used to estimate the KL divergence between policy and reference; should be batch size)
        """
        max_length = max(batch['target_combined_input_ids'].shape[1], batch['KL_combined_input_ids'].shape[1])
        concatenated_batch = {}

        for k in batch:
            if k.startswith('target') and isinstance(batch[k], torch.Tensor):
                pad_value = -100 if 'labels' in k else 0
                concatenated_key = k.replace('target', 'concatenated')
                concatenated_batch[concatenated_key] = pad_to_length(batch[k], max_length, pad_value=pad_value)
                
        for k in batch:
            if k.startswith('KL') and isinstance(batch[k], torch.Tensor):
                pad_value = -100 if 'labels' in k else 0
                concatenated_key = k.replace('KL', 'concatenated')
                concatenated_batch[concatenated_key] = torch.cat((
                    concatenated_batch[concatenated_key],
                    pad_to_length(batch[k], max_length, pad_value=pad_value),
                ), dim=0)

        all_logits = model(
            concatenated_batch[f'concatenated_combined_input_ids'],
            attention_mask=concatenated_batch[f'concatenated_combined_attention_mask']
        ).logits.to(self.policy_dtype)
        all_logps = get_batch_logps(all_logits, concatenated_batch[f'concatenated_labels'], average_log_prob=False)

        target_logps = all_logps[:batch['target_combined_input_ids'].shape[0]]
        KL_logps = all_logps[batch['target_combined_input_ids'].shape[0]:]

        assert target_logps.shape[0] == len(batch['status'])
        chosen_idx = [i for i in range(target_logps.shape[0]) if batch['status'][i] == 'chosen']
        rejected_idx = [i for i in range(target_logps.shape[0]) if batch['status'][i] == 'rejected']
        chosen_logps = target_logps[chosen_idx, ...]
        rejected_logps = target_logps[rejected_idx, ...]

        return chosen_logps, rejected_logps, KL_logps
    
    def get_batch_metrics(self, batch: Dict[str, Union[List, torch.LongTensor]], mode: str=None):
        """Compute the loss and other metrics for the given batch of inputs."""
        metrics = {}
        if mode is None: mode = self.config.mode

        policy_chosen_logps, policy_rejected_logps, policy_KL_logps = self.forward(self.policy, batch)
        with torch.no_grad():
            reference_chosen_logps, reference_rejected_logps, reference_KL_logps = self.forward(self.reference_model, batch)
        
        losses, chosen_rewards, rejected_rewards, KL = self.loss(
            policy_chosen_logps,
            policy_rejected_logps,
            policy_KL_logps,
            reference_chosen_logps,
            reference_rejected_logps,
            reference_KL_logps
        )

        combined_rewards = torch.cat((chosen_rewards.detach(), rejected_rewards.detach()), 0)
        combined_statuses = torch.Tensor([1] * len(chosen_rewards) + [0] * len(rejected_rewards)).to(self.device)

        all_rewards = all_gather_if_needed(combined_rewards, self.rank, self.world_size)
        all_statuses = all_gather_if_needed(combined_statuses, self.rank, self.world_size)
        all_KL = all_gather_if_needed(KL, self.rank, self.world_size)
        chosen_rewards_idx = [ i for i in range(len(all_statuses)) if all_statuses[i].item() == 1 ]
        rejected_rewards_idx = [ i for i in range(len(all_statuses)) if all_statuses[i].item() == 0 ]

        all_devices_losses = all_gather_if_needed(losses.detach(), self.rank, self.world_size)

        metrics[f'rewards_{mode}/chosen'] = all_rewards[chosen_rewards_idx].float().cpu().numpy().tolist()
        metrics[f'rewards_{mode}/rejected'] = all_rewards[rejected_rewards_idx].float().cpu().numpy().tolist()
        metrics[f'rewards_{mode}/margins'] = [(all_rewards[chosen_rewards_idx].mean().nan_to_num(0) - all_rewards[rejected_rewards_idx].mean().nan_to_num(0)).item()]
        metrics[f'rewards_{mode}/KL_estimate'] = all_KL.float().cpu().numpy().tolist()
        metrics[f'loss/{mode}'] = all_devices_losses.float().cpu().numpy().tolist()

        del policy_chosen_logps, policy_rejected_logps, policy_KL_logps, reference_chosen_logps, reference_rejected_logps, reference_KL_logps
        del combined_rewards, combined_statuses, all_rewards, all_statuses, chosen_rewards_idx, rejected_rewards_idx, all_devices_losses, all_KL

        return losses.mean(), metrics


class KTOZeroTrainer(UnpairedPreferenceTrainer):
    def loss(self,
             policy_chosen_logps: torch.FloatTensor,
             policy_rejected_logps: torch.FloatTensor,
             reference_chosen_logps: torch.FloatTensor,
             reference_rejected_logps: torch.FloatTensor) -> Tuple[torch.FloatTensor, torch.FloatTensor, torch.FloatTensor]:
        """Compute a variant of the Kahneman-Tversky loss where the reference point is 0 instead of the expected reward
        (i.e., the human reference point remains what it is at initialization, when policy = reference). This should NOT
        be used for purposes other than to understand the importance of the KL term.

        One can also think of this as a variant of unlikelihood training (Welleck et al., 2023). The purpose of this is to understand 
        the importance of the KL term in the standard variant of the KTO loss. We do *not* reecommend using this in practice as its
        performance is usually inferior. For each batch of n/2 chosen examples and n/2 rejected examples (belonging to n different 
        inputs), calculate the loss as follows.

        If generation y ~ p_chosen, where x' ~ are the examples with rejected generations, we have the 'chosen' loss:
            L(x, y) := 1 - sigmoid(beta * ([log p_policy(y|x) - log p_reference(y|x)] - 0))
        If generation y ~ p_rejected, , where x' ~ are the examples with chosen generations, we have the 'rejected' loss:
            L(x, y) := 1 - sigmoid(beta * (0 - [log p_policy(y|x) - log p_reference(y|x)]))
        """
        chosen_logratios = (policy_chosen_logps - reference_chosen_logps)
        rejected_logratios = (policy_rejected_logps - reference_rejected_logps)

        losses = torch.cat((1 - F.sigmoid(self.config.loss.beta * (chosen_logratios - 0)), 1 - F.sigmoid(self.config.loss.beta * (0 - rejected_logratios))), 0)

        chosen_rewards = self.config.loss.beta * (policy_chosen_logps - reference_chosen_logps).detach()
        rejected_rewards = self.config.loss.beta * (policy_rejected_logps - reference_rejected_logps).detach()

        return losses, chosen_rewards, rejected_rewards
