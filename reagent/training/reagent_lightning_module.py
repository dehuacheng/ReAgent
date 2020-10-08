#!/usr/bin/env python3

import logging

import pytorch_lightning as pl
import torch
from reagent.core.utils import lazy_property
from reagent.tensorboardX import SummaryWriterContext


logger = logging.getLogger(__name__)


class ReAgentLightningModule(pl.LightningModule):
    def __init__(self):
        super().__init__()
        self._training_step_generator = None
        self._reporter = pl.loggers.base.DummyExperiment()
        # For the generator API
        self._verified_steps = False
        # For summary_writer property
        self._summary_writer_logger = None
        self._summary_writer = None
        # To enable incremental training
        self.register_buffer("_next_stopping_epoch", None)
        self.register_buffer("_cleanly_stopped", None)
        self._next_stopping_epoch = torch.tensor([-1]).int()
        self._cleanly_stopped = torch.ones(1).bool()

    def set_reporter(self, reporter):
        if reporter is None:
            reporter = pl.loggers.base.DummyExperiment()
        self._reporter = reporter
        return self

    @property
    def reporter(self):
        return self._reporter

    def increase_next_stopping_epochs(self, num_epochs: int):
        self._next_stopping_epoch += num_epochs
        self._cleanly_stopped[0] = False
        return self

    def train_step_gen(self, training_batch, batch_idx: int):
        """
        Implement training step as generator here
        """
        raise NotImplementedError

    def soft_update_result(self) -> pl.TrainResult:
        """
        A dummy loss to trigger soft-update
        """
        one = torch.ones(1, requires_grad=True)
        # Create a fake graph to satisfy TrainResult
        # pyre-fixme[16]: Module `pl` has no attribute `TrainResult`.
        return pl.TrainResult(one + one)

    @property
    def summary_writer(self):
        """
        Accessor to TensorBoard's SummaryWriter
        """
        if self._summary_writer_logger is self.logger:
            # If self.logger doesn't change between call, then return cached result
            return self._summary_writer

        # Invalidate
        self._summary_writer = None
        self._summary_writer_logger = self.logger

        if isinstance(self.logger, pl.loggers.base.LoggerCollection):
            for logger in self.logger._logger_iterable:
                if isinstance(logger, pl.loggers.tensorboard.TensorBoardLogger):
                    self._summary_writer = logger.experiment
                    break
        elif isinstance(logger, pl.loggers.tensorboard.TensorBoardLogger):
            self._summary_writer = logger.experiment

        return self._summary_writer

    # pyre-fixme[14]: `training_step` overrides method defined in `LightningModule`
    #  inconsistently.
    # pyre-fixme[14]: `training_step` overrides method defined in `LightningModule`
    #  inconsistently.
    def training_step(self, batch, batch_idx: int, optimizer_idx: int):
        if self._training_step_generator is None:
            self._training_step_generator = self.train_step_gen(batch, batch_idx)

        ret = next(self._training_step_generator)

        if optimizer_idx == self._num_optimizing_steps - 1:
            if not self._verified_steps:
                try:
                    next(self._training_step_generator)
                except StopIteration:
                    self._verified_steps = True
                if not self._verified_steps:
                    raise RuntimeError("training_step_gen() yields too many times")
            self._training_step_generator = None
            SummaryWriterContext.increase_global_step()

        return ret

    @lazy_property
    def _num_optimizing_steps(self) -> int:
        return len(self.configure_optimizers())

    def training_epoch_end(self, training_step_outputs):
        # Flush the reporter
        self.reporter.flush(self.current_epoch)

        # Tell the trainer to stop.
        if self.current_epoch == self._next_stopping_epoch.item():
            self.trainer.should_stop = True
        return pl.TrainResult()


class StoppingEpochCallback(pl.Callback):
    """
    We use this callback to control the number of training epochs in incremental
    training. Epoch & step counts are not reset in the checkpoint. If we were to set
    `max_epochs` on the trainer, we would have to keep track of the previous `max_epochs`
    and add to it manually. This keeps the infomation in one place.

    Note that we need to set `_cleanly_stopped` back to True before saving the checkpoint.
    This is done in `ModelManager.save_trainer()`.
    """

    def __init__(self, num_epochs):
        super().__init__()
        self.num_epochs = num_epochs

    def on_pretrain_routine_end(self, trainer, pl_module):
        assert isinstance(pl_module, ReAgentLightningModule)
        cleanly_stopped = pl_module._cleanly_stopped.item()
        logger.info(f"cleanly stopped: {cleanly_stopped}")
        if cleanly_stopped:
            pl_module.increase_next_stopping_epochs(self.num_epochs)
