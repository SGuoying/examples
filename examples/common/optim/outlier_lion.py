from typing import Tuple, Optional, Callable
from examples.common.optim.outlier_detection import OutlierDetector
from examples.common.optim.lion import DecoupledLionW
import torch
from torch.optim.optimizer import Optimizer
import logging
import math
from composer.utils import dist

log = logging.getLogger(__name__)

# functions

class SkipLion(Optimizer):
    metric_functions = {
        'l2_norm/moment':
            lambda param, optim_state, step_tensor: torch.linalg.vector_norm(optim_state['exp_avg']),
        'l2_norm/param':
            lambda param, optim_state, step_tensor: torch.linalg.vector_norm(param.data),
        'l2_norm/update':
            lambda param, optim_state, step_tensor: torch.linalg.vector_norm(step_tensor),
        'l2_norm/grad':
            lambda param, optim_state, step_tensor: torch.linalg.vector_norm(param.grad),
        'cosine/update_grad':
            lambda param, optim_state, step_tensor: torch.nn.functional.cosine_similarity(
                param.grad.flatten(), step_tensor.flatten(), dim=0),
        'cosine/moment_grad':
            lambda param, optim_state, step_tensor: torch.nn.functional.cosine_similarity(
                param.grad.flatten(), optim_state['exp_avg'].flatten(), dim=0),
    }

    def __init__(
        self,
        params,
        lr: float = 1e-4,
        betas: Tuple[float, float] = (0.9, 0.99),
        weight_decay: float = 0.0,
        outlier_threshold = 10.0
    ):
        assert lr > 0.
        assert all([0. <= beta <= 1. for beta in betas])
        if weight_decay >= 1e-3:
            log.warning(
                f'You are using a high value of `weight_decay={weight_decay}` for the `DecoupledLionW` optimizer. Are you sure you want to do this? '
                f'Your model\'s weights will be multiplied by {1.0 - weight_decay} on every step!')

        defaults = dict(
            lr = lr,
            betas = betas,
            weight_decay = weight_decay
        )

        super().__init__(params, defaults)
        
        for group in self.param_groups:
            group['initial_lr'] = group['lr']
        self.outlier_threshold = outlier_threshold

    @staticmethod
    def lionw(p, grad, exp_avg, lr, initial_lr, wd, beta1, beta2) -> None:
        # stepweight decay
        if wd != 0:
            decay_factor = (lr / initial_lr) if initial_lr else 1.0
            p.data.mul_(1 - decay_factor * wd)

        # update is interpolation between gradient and momentum
        update = exp_avg.lerp(grad, 1 - beta1).sign_()
        p.add_(update, alpha = -lr)

        # momentum is interp b/w gradient and itself
        exp_avg.lerp_(grad, 1 - beta2)

    @torch.no_grad()
    def step(
        self,
        closure: Optional[Callable] = None
    ):

        loss = None
        if closure is not None:
            with torch.enable_grad():
                loss = closure()
        
        for group in self.param_groups:
            for p in filter(lambda p: p.grad is not None and p.requires_grad, group['params']):

                grad, lr, initial_lr, wd, beta1, beta2, state = p.grad, group['lr'], group['initial_lr'], group['weight_decay'], *group['betas'], self.state[p]

                # init state - exponential moving average of gradient values

                if len(state) == 0:
                    state['exp_avg'] = torch.zeros_like(p)
                    state['moment_tracker'] = OutlierDetector(self.outlier_threshold)
                    state['skipped_batches'] = torch.tensor(0.0)

                exp_avg = state['exp_avg']

                # determine if the new moment resulting from this grad would be an outlier
                moment_norm = torch.linalg.vector_norm(
                    exp_avg.lerp(grad, 1 - beta2)
                ) ** 2

                if dist.get_world_size() > 1:
                    dist.all_reduce(moment_norm, reduce_operation='SUM')
                moment_norm = math.sqrt(moment_norm)

                if state['moment_tracker'].insert_observation(moment_norm):
                    # skip completely
                    state['skipped_batches'] += 1.0
                    continue
                else:
                    self.lionw(
                        p,
                        grad,
                        exp_avg,
                        lr,
                        initial_lr,
                        wd,
                        beta1,
                        beta2
                    )

        return loss

    def dist_reduce_metrics(self, optimizer_metrics):
        for metric in optimizer_metrics:
            if metric.startswith('l2_norm'):
                reduced = optimizer_metrics[metric]
                if dist.get_world_size() > 1:
                    dist.all_reduce(reduced, reduce_operation='SUM')

                optimizer_metrics[metric] = math.sqrt(reduced)
            elif metric.startswith('cosine'):
                reduced = optimizer_metrics[metric]
                if dist.get_world_size() > 1:
                    dist.all_reduce(reduced, reduce_operation='SUM')

                _, vectors, layer = tuple(metric.split('/'))

                A, B = tuple(vectors.split('_'))

                A_reduced_norm = optimizer_metrics[f'l2_norm/{A}/{layer}']
                B_reduced_norm = optimizer_metrics[f'l2_norm/{B}/{layer}']
                optimizer_metrics[metric] = reduced / (A_reduced_norm * B_reduced_norm)
            elif metric.startswith('skipped_batches'):
                continue
            else:
                reduced = optimizer_metrics[metric]
                if dist.get_world_size() > 1:
                    dist.all_reduce(reduced, reduce_operation='SUM')
                optimizer_metrics[metric] = reduced / dist.get_world_size()

        return optimizer_metrics

    def pre_reduce_metrics(self, optimizer_metrics):
        """Preprocess metrics to reduce across ranks correctly."""
        # Sort L2 norms first so they are squared before other metrics, which depend on squared values
        metrics = optimizer_metrics.keys()
        metrics = sorted(metrics, key=lambda metric: 0 if 'l2_norm' in metric else 1)
        for metric in metrics:
            if metric.startswith('l2_norm'):
                # L2 norms need to be squared, before they are reduced via summation
                optimizer_metrics[metric] = optimizer_metrics[metric]**2
            elif metric.startswith('cosine'):
                _, vectors, layer = tuple(metric.split('/'))

                A, B = tuple(vectors.split('_'))

                # L2 norm would've been squared in previous branch
                A_rank_subset_norm = math.sqrt(optimizer_metrics[f'l2_norm/{A}/{layer}'])
                B_rank_subset_norm = math.sqrt(optimizer_metrics[f'l2_norm/{B}/{layer}'])

                optimizer_metrics[metric] *= A_rank_subset_norm * B_rank_subset_norm

        return optimizer_metrics

    def report_per_parameter_metrics(self, param: torch.Tensor, name: str, optimizer_metrics: dict):
        lr = self.param_groups[0]['lr']
        weight_decay = self.param_groups[0]['weight_decay']
        initial_lr = self.param_groups[0]['initial_lr']

        beta1, _ = self.param_groups[0]['betas']
        if param in self.state:
            param_optim_state = self.state[param]
            step_tensor =  param_optim_state['exp_avg'].clone().lerp_(param.grad, 1 - beta1).sign_().mul_(lr)
            decay_factor = (lr / initial_lr) if initial_lr else 1.0
            step_tensor.add_(param, alpha=-weight_decay * decay_factor)
            for metric in self.metric_functions:
                optimizer_metrics[f'{metric}/{name}'] = self.metric_functions[metric](param, param_optim_state,
                                                                                      step_tensor)

            optimizer_metrics[f'skipped_batches/{name}'] = param_optim_state['skipped_batches']

        return optimizer_metrics


class AdaBetaLion(Optimizer):
    metric_functions = {
        'l2_norm/moment':
            lambda param, optim_state, step_tensor: torch.linalg.vector_norm(optim_state['exp_avg']),
        'l2_norm/param':
            lambda param, optim_state, step_tensor: torch.linalg.vector_norm(param.data),
        'l2_norm/update':
            lambda param, optim_state, step_tensor: torch.linalg.vector_norm(step_tensor),
        'l2_norm/grad':
            lambda param, optim_state, step_tensor: torch.linalg.vector_norm(param.grad),
        'cosine/update_grad':
            lambda param, optim_state, step_tensor: torch.nn.functional.cosine_similarity(
                param.grad.flatten(), step_tensor.flatten(), dim=0),
        'cosine/moment_grad':
            lambda param, optim_state, step_tensor: torch.nn.functional.cosine_similarity(
                param.grad.flatten(), optim_state['exp_avg'].flatten(), dim=0),
    }

    def __init__(
        self,
        params,
        lr: float = 1e-4,
        betas: Tuple[float, float] = (0.9, 0.99),
        weight_decay: float = 0.0,
        outlier_threshold = 10.0,
        increase: bool = True,
        timeout: int = 50
    ):
        assert lr > 0.
        assert all([0. <= beta <= 1. for beta in betas])
        if weight_decay >= 1e-3:
            log.warning(
                f'You are using a high value of `weight_decay={weight_decay}` for the `DecoupledLionW` optimizer. Are you sure you want to do this? '
                f'Your model\'s weights will be multiplied by {1.0 - weight_decay} on every step!')

        defaults = dict(
            lr = lr,
            betas = betas,
            weight_decay = weight_decay
        )

        super().__init__(params, defaults)
        
        for group in self.param_groups:
            group['initial_lr'] = group['lr']
        self.outlier_threshold = outlier_threshold
        self.increase = increase
        self.timeout = timeout

    @staticmethod
    def lionw(p, grad, exp_avg, lr, initial_lr, wd, beta1, beta2) -> None:
        # stepweight decay
        if wd != 0:
            decay_factor = (lr / initial_lr) if initial_lr else 1.0
            p.data.mul_(1 - decay_factor * wd)

        # update is interpolation between gradient and momentum
        update = exp_avg.lerp(grad, 1 - beta1).sign_()
        p.add_(update, alpha = -lr)

        # momentum is interp b/w gradient and itself
        exp_avg.lerp_(grad, 1 - beta2)

    @staticmethod
    def adjust_betas(beta1, beta2, increase, num_times):
        scale = 0.5 if increase else 1.5

        beta1 = 1 - ((1-beta1) * (scale ** num_times))
        beta2 = 1 - ((1-beta2) * (scale ** num_times))
        return beta1, beta2

    @torch.no_grad()
    def step(
        self,
        closure: Optional[Callable] = None
    ):

        loss = None
        if closure is not None:
            with torch.enable_grad():
                loss = closure()
        
        for group in self.param_groups:
            for p in filter(lambda p: p.grad is not None and p.requires_grad, group['params']):

                grad, lr, initial_lr, wd, beta1, beta2, state = p.grad, group['lr'], group['initial_lr'], group['weight_decay'], *group['betas'], self.state[p]

                # init state - exponential moving average of gradient values

                if len(state) == 0:
                    state['exp_avg'] = torch.zeros_like(p)
                    state['moment_tracker'] = OutlierDetector(self.outlier_threshold)
                    state['outlier_timestamp'] = []
                    state['step'] = 0

                exp_avg = state['exp_avg']

                # determine if the new moment resulting from this grad would be an outlier
                moment_norm = torch.linalg.vector_norm(
                    exp_avg.lerp(grad, 1 - beta2)
                ) ** 2

                if dist.get_world_size() > 1:
                    dist.all_reduce(moment_norm, reduce_operation='SUM')
                moment_norm = math.sqrt(moment_norm)

                if state['moment_tracker'].insert_observation(moment_norm):
                    state['outlier_timestamp'].append(state['step'])
                
                removed = []
                for ts in state['outlier_timestamp']:
                    if state['step'] - ts > self.timeout:
                        removed.append(ts)
                
                for ts in removed:
                    state['outlier_timestamp'].remove(ts)
                
                beta1, beta2 = self.adjust_betas(beta1, beta2, self.increase, len(state['outlier_timestamp']))
                
                self.lionw(
                        p,
                        grad,
                        exp_avg,
                        lr,
                        initial_lr,
                        wd,
                        beta1,
                        beta2
                )
                state['step'] += 1


        return loss

    def dist_reduce_metrics(self, optimizer_metrics):
        for metric in optimizer_metrics:
            if metric.startswith('l2_norm'):
                reduced = optimizer_metrics[metric]
                if dist.get_world_size() > 1:
                    dist.all_reduce(reduced, reduce_operation='SUM')

                optimizer_metrics[metric] = math.sqrt(reduced)
            elif metric.startswith('cosine'):
                reduced = optimizer_metrics[metric]
                if dist.get_world_size() > 1:
                    dist.all_reduce(reduced, reduce_operation='SUM')

                _, vectors, layer = tuple(metric.split('/'))

                A, B = tuple(vectors.split('_'))

                A_reduced_norm = optimizer_metrics[f'l2_norm/{A}/{layer}']
                B_reduced_norm = optimizer_metrics[f'l2_norm/{B}/{layer}']
                optimizer_metrics[metric] = reduced / (A_reduced_norm * B_reduced_norm)
            elif metric.startswith('beta'):
                continue
            else:
                reduced = optimizer_metrics[metric]
                if dist.get_world_size() > 1:
                    dist.all_reduce(reduced, reduce_operation='SUM')
                optimizer_metrics[metric] = reduced / dist.get_world_size()

        return optimizer_metrics

    def pre_reduce_metrics(self, optimizer_metrics):
        """Preprocess metrics to reduce across ranks correctly."""
        # Sort L2 norms first so they are squared before other metrics, which depend on squared values
        metrics = optimizer_metrics.keys()
        metrics = sorted(metrics, key=lambda metric: 0 if 'l2_norm' in metric else 1)
        for metric in metrics:
            if metric.startswith('l2_norm'):
                # L2 norms need to be squared, before they are reduced via summation
                optimizer_metrics[metric] = optimizer_metrics[metric]**2
            elif metric.startswith('cosine'):
                _, vectors, layer = tuple(metric.split('/'))

                A, B = tuple(vectors.split('_'))

                # L2 norm would've been squared in previous branch
                A_rank_subset_norm = math.sqrt(optimizer_metrics[f'l2_norm/{A}/{layer}'])
                B_rank_subset_norm = math.sqrt(optimizer_metrics[f'l2_norm/{B}/{layer}'])

                optimizer_metrics[metric] *= A_rank_subset_norm * B_rank_subset_norm

        return optimizer_metrics

    def report_per_parameter_metrics(self, param: torch.Tensor, name: str, optimizer_metrics: dict):
        lr = self.param_groups[0]['lr']
        weight_decay = self.param_groups[0]['weight_decay']
        initial_lr = self.param_groups[0]['initial_lr']

        beta1, beta2 = self.param_groups[0]['betas']
        if param in self.state:
            param_optim_state = self.state[param]
            beta1, beta2 = self.adjust_betas(beta1, beta2, self.increase, len(param_optim_state['outlier_timestamp']))

            step_tensor =  param_optim_state['exp_avg'].clone().lerp_(param.grad, 1 - beta1).sign_().mul_(lr)
            decay_factor = (lr / initial_lr) if initial_lr else 1.0
            step_tensor.add_(param, alpha=-weight_decay * decay_factor)
            for metric in self.metric_functions:
                optimizer_metrics[f'{metric}/{name}'] = self.metric_functions[metric](param, param_optim_state,
                                                                                      step_tensor)

            optimizer_metrics[f'betas/beta1/{name}'] = torch.tensor(beta1)
            optimizer_metrics[f'betas/beta2/{name}'] = torch.tensor(beta2)

        return optimizer_metrics



class AdaLRLion(Optimizer):
    metric_functions = {
        'l2_norm/moment':
            lambda param, optim_state, step_tensor: torch.linalg.vector_norm(optim_state['exp_avg']),
        'l2_norm/param':
            lambda param, optim_state, step_tensor: torch.linalg.vector_norm(param.data),
        'l2_norm/update':
            lambda param, optim_state, step_tensor: torch.linalg.vector_norm(step_tensor),
        'l2_norm/grad':
            lambda param, optim_state, step_tensor: torch.linalg.vector_norm(param.grad),
        'cosine/update_grad':
            lambda param, optim_state, step_tensor: torch.nn.functional.cosine_similarity(
                param.grad.flatten(), step_tensor.flatten(), dim=0),
        'cosine/moment_grad':
            lambda param, optim_state, step_tensor: torch.nn.functional.cosine_similarity(
                param.grad.flatten(), optim_state['exp_avg'].flatten(), dim=0),
    }

    def __init__(
        self,
        params,
        lr: float = 1e-4,
        betas: Tuple[float, float] = (0.9, 0.99),
        weight_decay: float = 0.0,
        outlier_threshold = 10.0,
        timeout: int = 100,
        lr_penalty: float = .707,
        min_scale: float = 1e-4
    ):
        assert lr > 0.
        assert all([0. <= beta <= 1. for beta in betas])
        if weight_decay >= 1e-3:
            log.warning(
                f'You are using a high value of `weight_decay={weight_decay}` for the `DecoupledLionW` optimizer. Are you sure you want to do this? '
                f'Your model\'s weights will be multiplied by {1.0 - weight_decay} on every step!')

        defaults = dict(
            lr = lr,
            betas = betas,
            weight_decay = weight_decay
        )

        super().__init__(params, defaults)
        
        for group in self.param_groups:
            group['initial_lr'] = group['lr']
        self.outlier_threshold = outlier_threshold
        self.timeout = timeout
        self.lr_penalty = lr_penalty
        self.min_scale = min_scale

    @staticmethod
    def lionw(p, grad, exp_avg, lr, initial_lr, wd, beta1, beta2) -> None:
        # stepweight decay
        if wd != 0:
            decay_factor = (lr / initial_lr) if initial_lr else 1.0
            p.data.mul_(1 - decay_factor * wd)

        # update is interpolation between gradient and momentum
        update = exp_avg.lerp(grad, 1 - beta1).sign_()
        p.add_(update, alpha = -lr)

        # momentum is interp b/w gradient and itself
        exp_avg.lerp_(grad, 1 - beta2)

    @staticmethod
    def adjust_lr(lr, lr_penalty, num_times, min_scale):
        return lr * max(min_scale, lr_penalty ** num_times)

    @torch.no_grad()
    def step(
        self,
        closure: Optional[Callable] = None
    ):

        loss = None
        if closure is not None:
            with torch.enable_grad():
                loss = closure()
        
        for group in self.param_groups:
            for p in filter(lambda p: p.grad is not None and p.requires_grad, group['params']):

                grad, lr, initial_lr, wd, beta1, beta2, state = p.grad, group['lr'], group['initial_lr'], group['weight_decay'], *group['betas'], self.state[p]

                # init state - exponential moving average of gradient values

                if len(state) == 0:
                    state['exp_avg'] = torch.zeros_like(p)
                    state['moment_tracker'] = OutlierDetector(self.outlier_threshold)
                    state['outlier_timestamp'] = []
                    state['step'] = 0

                exp_avg = state['exp_avg']

                # determine if the new moment resulting from this grad would be an outlier
                moment_norm = torch.linalg.vector_norm(
                    exp_avg.lerp(grad, 1 - beta2)
                ) ** 2

                if dist.get_world_size() > 1:
                    dist.all_reduce(moment_norm, reduce_operation='SUM')
                moment_norm = math.sqrt(moment_norm)

                if state['moment_tracker'].insert_observation(moment_norm):
                    state['outlier_timestamp'].append(state['step'])
                
                removed = []
                for ts in state['outlier_timestamp']:
                    if state['step'] - ts > self.timeout:
                        removed.append(ts)
                
                for ts in removed:
                    state['outlier_timestamp'].remove(ts)
                
                lr = self.adjust_lr(lr, self.lr_penalty, len(state['outlier_timestamp']), self.min_scale)
                self.lionw(
                        p,
                        grad,
                        exp_avg,
                        lr,
                        initial_lr,
                        wd,
                        beta1,
                        beta2
                )
                state['step'] += 1


        return loss

    def dist_reduce_metrics(self, optimizer_metrics):
        for metric in optimizer_metrics:
            if metric.startswith('l2_norm'):
                reduced = optimizer_metrics[metric]
                if dist.get_world_size() > 1:
                    dist.all_reduce(reduced, reduce_operation='SUM')

                optimizer_metrics[metric] = math.sqrt(reduced)
            elif metric.startswith('cosine'):
                reduced = optimizer_metrics[metric]
                if dist.get_world_size() > 1:
                    dist.all_reduce(reduced, reduce_operation='SUM')

                _, vectors, layer = tuple(metric.split('/'))

                A, B = tuple(vectors.split('_'))

                A_reduced_norm = optimizer_metrics[f'l2_norm/{A}/{layer}']
                B_reduced_norm = optimizer_metrics[f'l2_norm/{B}/{layer}']
                optimizer_metrics[metric] = reduced / (A_reduced_norm * B_reduced_norm)
            elif metric.startswith('layerwise_lr'):
                continue
            else:
                reduced = optimizer_metrics[metric]
                if dist.get_world_size() > 1:
                    dist.all_reduce(reduced, reduce_operation='SUM')
                optimizer_metrics[metric] = reduced / dist.get_world_size()

        return optimizer_metrics

    def pre_reduce_metrics(self, optimizer_metrics):
        """Preprocess metrics to reduce across ranks correctly."""
        # Sort L2 norms first so they are squared before other metrics, which depend on squared values
        metrics = optimizer_metrics.keys()
        metrics = sorted(metrics, key=lambda metric: 0 if 'l2_norm' in metric else 1)
        for metric in metrics:
            if metric.startswith('l2_norm'):
                # L2 norms need to be squared, before they are reduced via summation
                optimizer_metrics[metric] = optimizer_metrics[metric]**2
            elif metric.startswith('cosine'):
                _, vectors, layer = tuple(metric.split('/'))

                A, B = tuple(vectors.split('_'))

                # L2 norm would've been squared in previous branch
                A_rank_subset_norm = math.sqrt(optimizer_metrics[f'l2_norm/{A}/{layer}'])
                B_rank_subset_norm = math.sqrt(optimizer_metrics[f'l2_norm/{B}/{layer}'])

                optimizer_metrics[metric] *= A_rank_subset_norm * B_rank_subset_norm

        return optimizer_metrics

    def report_per_parameter_metrics(self, param: torch.Tensor, name: str, optimizer_metrics: dict):
        lr = self.param_groups[0]['lr']
        weight_decay = self.param_groups[0]['weight_decay']
        initial_lr = self.param_groups[0]['initial_lr']

        beta1, _ = self.param_groups[0]['betas']
        if param in self.state:
            param_optim_state = self.state[param]
            layerwise_lr = self.adjust_lr(lr, self.lr_penalty, len(param_optim_state['outlier_timestamp']), self.min_scale)

            step_tensor =  param_optim_state['exp_avg'].clone().lerp_(param.grad, 1 - beta1).sign_().mul_(lr)
            decay_factor = (lr / initial_lr) if initial_lr else 1.0
            step_tensor.add_(param, alpha=-weight_decay * decay_factor)
            for metric in self.metric_functions:
                optimizer_metrics[f'{metric}/{name}'] = self.metric_functions[metric](param, param_optim_state,
                                                                                      step_tensor)

            optimizer_metrics[f'layerwise_lr/{name}'] = torch.tensor(layerwise_lr)

        return optimizer_metrics




class ClipLion(Optimizer):
    metric_functions = {
        'l2_norm/moment':
            lambda param, optim_state, step_tensor: torch.linalg.vector_norm(optim_state['exp_avg']),
        'l2_norm/param':
            lambda param, optim_state, step_tensor: torch.linalg.vector_norm(param.data),
        'l2_norm/update':
            lambda param, optim_state, step_tensor: torch.linalg.vector_norm(step_tensor),
        'l2_norm/grad':
            lambda param, optim_state, step_tensor: torch.linalg.vector_norm(param.grad),
        'cosine/update_grad':
            lambda param, optim_state, step_tensor: torch.nn.functional.cosine_similarity(
                param.grad.flatten(), step_tensor.flatten(), dim=0),
        'cosine/moment_grad':
            lambda param, optim_state, step_tensor: torch.nn.functional.cosine_similarity(
                param.grad.flatten(), optim_state['exp_avg'].flatten(), dim=0),
    }

    def __init__(
        self,
        params,
        lr: float = 1e-4,
        betas: Tuple[float, float] = (0.9, 0.99),
        weight_decay: float = 0.0,
        outlier_threshold = 5.0
    ):
        assert lr > 0.
        assert all([0. <= beta <= 1. for beta in betas])
        if weight_decay >= 1e-3:
            log.warning(
                f'You are using a high value of `weight_decay={weight_decay}` for the `DecoupledLionW` optimizer. Are you sure you want to do this? '
                f'Your model\'s weights will be multiplied by {1.0 - weight_decay} on every step!')

        defaults = dict(
            lr = lr,
            betas = betas,
            weight_decay = weight_decay
        )

        super().__init__(params, defaults)
        
        for group in self.param_groups:
            group['initial_lr'] = group['lr']
        self.outlier_threshold = outlier_threshold

    @staticmethod
    def lionw(p, grad, exp_avg, lr, initial_lr, wd, beta1, beta2) -> None:
        # stepweight decay
        if wd != 0:
            decay_factor = (lr / initial_lr) if initial_lr else 1.0
            p.data.mul_(1 - decay_factor * wd)

        # update is interpolation between gradient and momentum
        update = exp_avg.lerp(grad, 1 - beta1).sign_()
        p.add_(update, alpha = -lr)

        # momentum is interp b/w gradient and itself
        exp_avg.lerp_(grad, 1 - beta2)

    @torch.no_grad()
    def step(
        self,
        closure: Optional[Callable] = None
    ):

        loss = None
        if closure is not None:
            with torch.enable_grad():
                loss = closure()
        
        for group in self.param_groups:
            for p in filter(lambda p: p.grad is not None and p.requires_grad, group['params']):

                grad, lr, initial_lr, wd, beta1, beta2, state = p.grad, group['lr'], group['initial_lr'], group['weight_decay'], *group['betas'], self.state[p]

                # init state - exponential moving average of gradient values

                if len(state) == 0:
                    state['exp_avg'] = torch.zeros_like(p)
                    state['grad_tracker'] = OutlierDetector(self.outlier_threshold)
                    state['clipped_batches'] = torch.tensor(0.0)

                exp_avg = state['exp_avg']

                # determine if the new moment resulting from this grad would be an outlier
                grad_norm = torch.linalg.vector_norm(
                    grad
                ) ** 2

                if dist.get_world_size() > 1:
                    dist.all_reduce(grad_norm, reduce_operation='SUM')
                grad_norm = math.sqrt(grad_norm)

                if state['grad_tracker'].insert_observation(grad_norm):
                    # skip completely
                    state['clipped_batches'] += 1.0
                    clip_norm = state['grad_tracker'].get_slow_mva() * self.outlier_threshold
                    grad = grad.div(grad_norm).mul_(clip_norm) 
                
                self.lionw(
                        p,
                        grad,
                        exp_avg,
                        lr,
                        initial_lr,
                        wd,
                        beta1,
                        beta2
                )

        return loss

    def dist_reduce_metrics(self, optimizer_metrics):
        for metric in optimizer_metrics:
            if metric.startswith('l2_norm'):
                reduced = optimizer_metrics[metric]
                if dist.get_world_size() > 1:
                    dist.all_reduce(reduced, reduce_operation='SUM')

                optimizer_metrics[metric] = math.sqrt(reduced)
            elif metric.startswith('cosine'):
                reduced = optimizer_metrics[metric]
                if dist.get_world_size() > 1:
                    dist.all_reduce(reduced, reduce_operation='SUM')

                _, vectors, layer = tuple(metric.split('/'))

                A, B = tuple(vectors.split('_'))

                A_reduced_norm = optimizer_metrics[f'l2_norm/{A}/{layer}']
                B_reduced_norm = optimizer_metrics[f'l2_norm/{B}/{layer}']
                optimizer_metrics[metric] = reduced / (A_reduced_norm * B_reduced_norm)
            elif metric.startswith('clipped_batches'):
                continue
            else:
                reduced = optimizer_metrics[metric]
                if dist.get_world_size() > 1:
                    dist.all_reduce(reduced, reduce_operation='SUM')
                optimizer_metrics[metric] = reduced / dist.get_world_size()

        return optimizer_metrics

    def pre_reduce_metrics(self, optimizer_metrics):
        """Preprocess metrics to reduce across ranks correctly."""
        # Sort L2 norms first so they are squared before other metrics, which depend on squared values
        metrics = optimizer_metrics.keys()
        metrics = sorted(metrics, key=lambda metric: 0 if 'l2_norm' in metric else 1)
        for metric in metrics:
            if metric.startswith('l2_norm'):
                # L2 norms need to be squared, before they are reduced via summation
                optimizer_metrics[metric] = optimizer_metrics[metric]**2
            elif metric.startswith('cosine'):
                _, vectors, layer = tuple(metric.split('/'))

                A, B = tuple(vectors.split('_'))

                # L2 norm would've been squared in previous branch
                A_rank_subset_norm = math.sqrt(optimizer_metrics[f'l2_norm/{A}/{layer}'])
                B_rank_subset_norm = math.sqrt(optimizer_metrics[f'l2_norm/{B}/{layer}'])

                optimizer_metrics[metric] *= A_rank_subset_norm * B_rank_subset_norm

        return optimizer_metrics

    def report_per_parameter_metrics(self, param: torch.Tensor, name: str, optimizer_metrics: dict):
        lr = self.param_groups[0]['lr']
        weight_decay = self.param_groups[0]['weight_decay']
        initial_lr = self.param_groups[0]['initial_lr']

        beta1, _ = self.param_groups[0]['betas']
        if param in self.state:
            param_optim_state = self.state[param]
            step_tensor =  param_optim_state['exp_avg'].clone().lerp_(param.grad, 1 - beta1).sign_().mul_(lr)
            decay_factor = (lr / initial_lr) if initial_lr else 1.0
            step_tensor.add_(param, alpha=-weight_decay * decay_factor)
            for metric in self.metric_functions:
                optimizer_metrics[f'{metric}/{name}'] = self.metric_functions[metric](param, param_optim_state,
                                                                                      step_tensor)

            optimizer_metrics[f'clipped_batches/{name}'] = param_optim_state['clipped_batches']

        return optimizer_metrics



class FullyAdaptiveLion(Optimizer):
    metric_functions = {
        'l2_norm/moment':
            lambda param, optim_state, step_tensor: torch.linalg.vector_norm(optim_state['exp_avg']),
        'l2_norm/param':
            lambda param, optim_state, step_tensor: torch.linalg.vector_norm(param.data),
        'l2_norm/update':
            lambda param, optim_state, step_tensor: torch.linalg.vector_norm(step_tensor),
        'l2_norm/grad':
            lambda param, optim_state, step_tensor: torch.linalg.vector_norm(param.grad),
        'cosine/update_grad':
            lambda param, optim_state, step_tensor: torch.nn.functional.cosine_similarity(
                param.grad.flatten(), step_tensor.flatten(), dim=0),
        'cosine/moment_grad':
            lambda param, optim_state, step_tensor: torch.nn.functional.cosine_similarity(
                param.grad.flatten(), optim_state['exp_avg'].flatten(), dim=0),
    }

    def __init__(
        self,
        params,
        lr: float = 1e-4,
        betas: Tuple[float, float] = (0.9, 0.99),
        outlier_threshold = 7.5,
        timeout: int = 20,
        param_adjustment: float = .5
    ):
        print(lr)
        assert lr > 0.
        assert all([0. <= beta <= 1. for beta in betas])
       

        defaults = dict(
            lr = lr,
            betas = betas,
        )

        super().__init__(params, defaults)
        
        for group in self.param_groups:
            group['initial_lr'] = group['lr']
        self.outlier_threshold = outlier_threshold
        self.timeout = timeout
        self.param_adjustment = param_adjustment
        self.param_init_norm = {}
        self.param_wd_scaling = {}


    @staticmethod
    def adjust_param(lr, param_adjustment, num_times):
        return lr * (param_adjustment ** num_times)

   
    @staticmethod
    def lionw(p, grad, exp_avg, lr, initial_lr, wd, beta1, beta2) -> None:
        # stepweight decay
        if wd != 0:
            decay_factor = (lr / initial_lr) if initial_lr else 1.0
            p.data.mul_(1 - decay_factor * wd)

        # update is interpolation between gradient and momentum
        update = exp_avg.lerp(grad, 1 - beta1).sign_()
        p.add_(update, alpha = -lr)

        # momentum is interp b/w gradient and itself
        exp_avg.lerp_(grad, 1 - beta2)


    @torch.no_grad()
    def step(
        self,
        closure: Optional[Callable] = None
    ):

        loss = None
        if closure is not None:
            with torch.enable_grad():
                loss = closure()
        tmp_param_init_norm = {}
        tmp_param_current_norm = {}
        for group in self.param_groups:
            for p in filter(lambda p: p.grad is not None and p.requires_grad, group['params']):
                if p not in self.param_init_norm:
                    tmp_param_init_norm[p] = torch.linalg.vector_norm(p.data.detach()) ** 2

                grad, lr, initial_lr, beta1, beta2, state = p.grad, group['lr'], group['initial_lr'], *group['betas'], self.state[p]

                # init state - exponential moving average of gradient values

                if len(state) == 0:
                    state['exp_avg'] = torch.zeros_like(p)
                    state['moment_tracker'] = OutlierDetector(self.outlier_threshold)
                    state['outlier_timestamp'] = []
                    state['step'] = 0

                exp_avg = state['exp_avg']

                # determine if the new moment resulting from this grad would be an outlier
                moment_norm = torch.linalg.vector_norm(
                    exp_avg.lerp(grad, 1 - beta2)
                ) ** 2

                if dist.get_world_size() > 1:
                    dist.all_reduce(moment_norm, reduce_operation='SUM')
                moment_norm = math.sqrt(moment_norm)

                if state['moment_tracker'].insert_observation(moment_norm):
                    state['outlier_timestamp'].append(state['step'])
                
                removed = []
                for ts in state['outlier_timestamp']:
                    if state['step'] - ts > self.timeout:
                        removed.append(ts)
                
                for ts in removed:
                    state['outlier_timestamp'].remove(ts)
                
                lr = self.adjust_param(lr, self.param_adjustment, len(state['outlier_timestamp']))
                beta1 = self.adjust_param(beta1, self.param_adjustment, len(state['outlier_timestamp']))
                beta2 = self.adjust_param(beta1, self.param_adjustment, len(state['outlier_timestamp']))
                wd = lr * self.param_wd_scaling.get(p, 0.0)
                self.lionw(
                        p,
                        grad,
                        exp_avg,
                        lr,
                        initial_lr,
                        wd,
                        beta1,
                        beta2
                )
                state['step'] += 1
                tmp_param_current_norm[p] = torch.linalg.vector_norm(p.data.detach()) ** 2

        # calculate all_reduce of initial param norm if applicable
        for p in tmp_param_init_norm:
            reduced = tmp_param_init_norm[p]
            if dist.get_world_size() > 1:
                dist.all_reduce(reduced, reduce_operation='SUM')
            self.param_init_norm[p] = math.sqrt(reduced)

        # calculate all_reduce of current param norm if applicable
        for p in tmp_param_current_norm:
            reduced = tmp_param_current_norm[p]
            if dist.get_world_size() > 1:
                dist.all_reduce(reduced, reduce_operation='SUM')
            tmp_param_current_norm[p] = math.sqrt(reduced)

        # determine new per-param WD pctg of LR based on current param norm
        for p in tmp_param_current_norm:
            p_0 = self.param_init_norm[p]
            p_t = tmp_param_current_norm[p]
            wd_scale = torch.tensor((p_t-p_0)/p_0)
            self.param_wd_scaling[p] = wd_scale

        return loss

    
    def dist_reduce_metrics(self, optimizer_metrics):
        for metric in optimizer_metrics:
            if metric.startswith('l2_norm'):
                reduced = optimizer_metrics[metric]
                if dist.get_world_size() > 1:
                    dist.all_reduce(reduced, reduce_operation='SUM')

                optimizer_metrics[metric] = math.sqrt(reduced)
            elif metric.startswith('cosine'):
                reduced = optimizer_metrics[metric]
                if dist.get_world_size() > 1:
                    dist.all_reduce(reduced, reduce_operation='SUM')

                _, vectors, layer = tuple(metric.split('/'))

                A, B = tuple(vectors.split('_'))

                A_reduced_norm = optimizer_metrics[f'l2_norm/{A}/{layer}']
                B_reduced_norm = optimizer_metrics[f'l2_norm/{B}/{layer}']
                optimizer_metrics[metric] = reduced / (A_reduced_norm * B_reduced_norm)
            
        return optimizer_metrics

    def pre_reduce_metrics(self, optimizer_metrics):
        """Preprocess metrics to reduce across ranks correctly."""
        # Sort L2 norms first so they are squared before other metrics, which depend on squared values
        metrics = optimizer_metrics.keys()
        metrics = sorted(metrics, key=lambda metric: 0 if 'l2_norm' in metric else 1)
        for metric in metrics:
            if metric.startswith('l2_norm'):
                # L2 norms need to be squared, before they are reduced via summation
                optimizer_metrics[metric] = optimizer_metrics[metric]**2
            elif metric.startswith('cosine'):
                _, vectors, layer = tuple(metric.split('/'))

                A, B = tuple(vectors.split('_'))

                # L2 norm would've been squared in previous branch
                A_rank_subset_norm = math.sqrt(optimizer_metrics[f'l2_norm/{A}/{layer}'])
                B_rank_subset_norm = math.sqrt(optimizer_metrics[f'l2_norm/{B}/{layer}'])

                optimizer_metrics[metric] *= A_rank_subset_norm * B_rank_subset_norm


        return optimizer_metrics


    def report_per_parameter_metrics(self, param: torch.Tensor, name: str, optimizer_metrics: dict):
        lr = self.param_groups[0]['lr']
        initial_lr = self.param_groups[0]['initial_lr']

        beta1, beta2 = self.param_groups[0]['betas']
        if param in self.state:
            param_optim_state = self.state[param]
            layerwise_lr = self.adjust_param(lr, self.param_adjustment, len(param_optim_state['outlier_timestamp']))
            beta1 = self.adjust_param(beta1, self.param_adjustment, len(param_optim_state['outlier_timestamp']))
            beta2 = self.adjust_param(beta1, self.param_adjustment, len(param_optim_state['outlier_timestamp']))
            wd = layerwise_lr * self.param_wd_scaling.get(param, 0.0)


            step_tensor =  param_optim_state['exp_avg'].clone().lerp_(param.grad, 1 - beta1).sign_().mul_(layerwise_lr)
            decay_factor = (layerwise_lr / initial_lr) if initial_lr else 1.0
            step_tensor.add_(param, alpha=-wd * decay_factor)
            for metric in self.metric_functions:
                optimizer_metrics[f'{metric}/{name}'] = self.metric_functions[metric](param, param_optim_state,
                                                                                      step_tensor)

            optimizer_metrics[f'layerwise_lr/{name}'] = torch.tensor(layerwise_lr)
            optimizer_metrics[f'betas/beta1/{name}'] = torch.tensor(beta1)
            optimizer_metrics[f'betas/beta2/{name}'] = torch.tensor(beta2)
            optimizer_metrics[f'wd_scaling/{name}'] = self.param_wd_scaling.get(param, torch.tensor(0.0))

        return optimizer_metrics
