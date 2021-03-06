# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Model Checkpointing
===================

Automatically save model checkpoints during training.

"""

import os
import re
from copy import deepcopy
from typing import Any, Dict, Optional

import numpy as np
import torch
from pytorch_lightning import _logger as log
from pytorch_lightning.callbacks.base import Callback
from pytorch_lightning.utilities import rank_zero_only, rank_zero_warn
from pytorch_lightning.utilities.cloud_io import get_filesystem
from pytorch_lightning.utilities.exceptions import MisconfigurationException


class ModelCheckpoint(Callback):
    r"""
    Save the model after every epoch by monitoring a quantity.

    After training finishes, use :attr:`best_model_path` to retrieve the path to the
    best checkpoint file and :attr:`best_model_score` to retrieve its score.

    Args:
        filepath: path to save the model file.
            Can contain named formatting options to be auto-filled.

            Example::

                # custom path
                # saves a file like: my/path/epoch=0.ckpt
                >>> checkpoint_callback = ModelCheckpoint('my/path/')

                # save any arbitrary metrics like `val_loss`, etc. in name
                # saves a file like: my/path/epoch=2-val_loss=0.02-other_metric=0.03.ckpt
                >>> checkpoint_callback = ModelCheckpoint(
                ...     filepath='my/path/{epoch}-{val_loss:.2f}-{other_metric:.2f}'
                ... )

            By default, filepath is `None` and will be set at runtime to the location
            specified by :class:`~pytorch_lightning.trainer.trainer.Trainer`'s
            :paramref:`~pytorch_lightning.trainer.trainer.Trainer.default_root_dir` or
            :paramref:`~pytorch_lightning.trainer.trainer.Trainer.weights_save_path` arguments,
            and if the Trainer uses a logger, the path will also contain logger name and version.

        monitor: quantity to monitor. By default it is None which saves a checkpoint only for the last epoch
        verbose: verbosity mode. Default: ``False``.
        save_last: always saves the model at the end of the epoch. Default: ``False``.
        save_top_k: if ``save_top_k == k``,
            the best k models according to
            the quantity monitored will be saved.
            if ``save_top_k == 0``, no models are saved.
            if ``save_top_k == -1``, all models are saved.
            Please note that the monitors are checked every `period` epochs.
            if ``save_top_k >= 2`` and the callback is called multiple
            times inside an epoch, the name of the saved file will be
            appended with a version count starting with `v0`.
        mode: one of {auto, min, max}.
            If ``save_top_k != 0``, the decision
            to overwrite the current save file is made
            based on either the maximization or the
            minimization of the monitored quantity. For `val_acc`,
            this should be `max`, for `val_loss` this should
            be `min`, etc. In `auto` mode, the direction is
            automatically inferred from the name of the monitored quantity.
        save_weights_only: if ``True``, then only the model's weights will be
            saved (``model.save_weights(filepath)``), else the full model
            is saved (``model.save(filepath)``).
        period: Interval (number of epochs) between checkpoints.

    Example::

        >>> from pytorch_lightning import Trainer
        >>> from pytorch_lightning.callbacks import ModelCheckpoint

        # saves checkpoints to 'my/path/' at every epoch
        >>> checkpoint_callback = ModelCheckpoint(filepath='my/path/')
        >>> trainer = Trainer(checkpoint_callback=checkpoint_callback)

        # save epoch and val_loss in name
        # saves a file like: my/path/sample-mnist-epoch=02-val_loss=0.32.ckpt
        >>> checkpoint_callback = ModelCheckpoint(monitor='val_loss',
        ...     filepath='my/path/sample-mnist-{epoch:02d}-{val_loss:.2f}'
        ... )

        # retrieve the best checkpoint after training
        checkpoint_callback = ModelCheckpoint(filepath='my/path/')
        trainer = Trainer(checkpoint_callback=checkpoint_callback)
        model = ...
        trainer.fit(model)
        checkpoint_callback.best_model_path
    """

    CHECKPOINT_JOIN_CHAR = "-"
    CHECKPOINT_NAME_LAST = "last"
    CHECKPOINT_STATE_BEST_SCORE = "checkpoint_callback_best_model_score"
    CHECKPOINT_STATE_BEST_PATH = "checkpoint_callback_best_model_path"

    def __init__(
        self,
        filepath: Optional[str] = None,
        monitor: Optional[str] = None,
        verbose: bool = False,
        save_last: bool = False,
        save_top_k: int = 1,
        save_weights_only: bool = False,
        mode: str = "auto",
        period: int = 1,
        prefix: str = "",
    ):
        super().__init__()
        self.monitor = monitor
        self.verbose = verbose
        self.save_last = save_last
        self.save_top_k = save_top_k
        self.save_weights_only = save_weights_only
        self.period = period
        self.epoch_last_check = None
        self.prefix = prefix
        self.best_k_models = {}
        self.kth_best_model_path = ""
        self.best_model_score = 0
        self.best_model_path = ""
        self.last_model_path = ""
        self.save_function = None
        self.warned_result_obj = False

        self.__init_monitor_mode(monitor, mode)
        self.__init_ckpt_dir(filepath, save_top_k)
        self.__validate_init_configuration()

    def on_pretrain_routine_start(self, trainer, pl_module):
        """
        When pretrain routine starts we build the ckpt dir on the fly
        """
        self.__resolve_ckpt_dir(trainer, pl_module)

    def on_validation_end(self, trainer, pl_module):
        """
        checkpoints can be saved at the end of the val loop
        """
        self.save_checkpoint(trainer, pl_module)

    def on_save_checkpoint(self, trainer, pl_module) -> Dict[str, Any]:
        return {
            "best_model_score": self.best_model_score,
            "best_model_path": self.best_model_path,
        }

    def on_load_checkpoint(self, checkpointed_state: Dict[str, Any]):
        self.best_model_score = checkpointed_state["best_model_score"]
        self.best_model_path = checkpointed_state["best_model_path"]

    @rank_zero_only
    def save_checkpoint(self, trainer, pl_module):
        """
        Performs the main logic around saving a checkpoint
        """
        # only run on main process
        if trainer.global_rank != 0:
            return

        # no models are saved
        if self.save_top_k == 0:
            return

        # don't save anything during sanity check
        if trainer.running_sanity_check:
            return

        # skip this epoch
        if self._should_skip_epoch(trainer):
            return

        self._add_backward_monitor_support(trainer)
        self._validate_monitor_key(trainer)

        epoch = trainer.current_epoch

        # track epoch when ckpt was last checked
        self.epoch_last_check = trainer.current_epoch

        # what can be monitored
        monitor_candidates = self._monitor_candidates(trainer)

        # ie: path/val_loss=0.5.ckpt
        filepath = self._get_metric_interpolated_filepath_name(epoch, monitor_candidates)

        # callback supports multiple simultaneous modes
        # here we call each mode sequentially
        # Mode 1: save the last checkpoint
        self._save_last_checkpoint(trainer, pl_module, epoch, monitor_candidates, filepath)

        # Mode 2: save all checkpoints OR only the top k
        if self.monitor is not None:
            if self.save_top_k == -1:
                self._save_all_checkpoints(trainer, pl_module, epoch, filepath)
            else:
                self._save_top_k_checkpoints(monitor_candidates, trainer, pl_module, epoch, filepath)

    def __validate_init_configuration(self):
        if self.save_top_k != 1 and self.monitor is None:
            raise MisconfigurationException('To save checkpoints for a top_k metric, '
                                            'ModelCheckpoint(monitor) cannot be None')

    def __init_ckpt_dir(self, filepath, save_top_k):
        self._fs = get_filesystem(filepath if filepath is not None else "")
        if save_top_k > 0 and filepath is not None:
            if self._fs.isdir(filepath) and len(self._fs.ls(filepath)) > 0:
                rank_zero_warn(
                    f"Checkpoint directory {filepath} exists and is not empty with save_top_k != 0."
                    " All files in this directory will be deleted when a checkpoint is saved!"
                )

        if not filepath:  # will be determined by trainer at runtime
            self.dirpath, self.filename = None, None
        else:
            if self._fs.isdir(filepath):
                self.dirpath, self.filename = filepath, None
            else:
                if self._fs.protocol == "file":  # dont normalize remote paths
                    filepath = os.path.realpath(filepath)
                self.dirpath, self.filename = os.path.split(filepath)
            self._fs.makedirs(self.dirpath, exist_ok=True)

    def __init_monitor_mode(self, monitor, mode):
        torch_inf = torch.tensor(np.Inf)
        mode_dict = {
            "min": (torch_inf, "min"),
            "max": (-torch_inf, "max"),
            "auto": (-torch_inf, "max")
            if monitor is not None and ("acc" in monitor or monitor.startswith("fmeasure"))
            else (torch_inf, "min"),
        }

        if mode not in mode_dict:
            rank_zero_warn(
                f"ModelCheckpoint mode {mode} is unknown, " f"fallback to auto mode.",
                RuntimeWarning,
            )
            mode = "auto"

        self.kth_value, self.mode = mode_dict[mode]

    def _del_model(self, filepath: str):
        if self._fs.exists(filepath):
            self._fs.rm(filepath)

    def _save_model(self, filepath: str, trainer, pl_module):

        # in debugging, track when we save checkpoints
        trainer.dev_debugger.track_checkpointing_history(filepath)

        # make paths
        self._fs.makedirs(os.path.dirname(filepath), exist_ok=True)

        # delegate the saving to the model
        if self.save_function is not None:
            self.save_function(filepath, self.save_weights_only)
        else:
            raise ValueError(".save_function() not set")

    def check_monitor_top_k(self, current) -> bool:
        less_than_k_models = len(self.best_k_models) < self.save_top_k
        if less_than_k_models:
            return True

        if not isinstance(current, torch.Tensor):
            rank_zero_warn(
                f"{current} is supposed to be a `torch.Tensor`. Saving checkpoint may not work correctly."
                f" HINT: check the value of {self.monitor} in your validation loop",
                RuntimeWarning,
            )
            current = torch.tensor(current)

        monitor_op = {"min": torch.lt, "max": torch.gt}[self.mode]

        val = monitor_op(current, self.best_k_models[self.kth_best_model_path])
        return val

    @classmethod
    def _format_checkpoint_name(
        cls,
        filename: Optional[str],
        epoch: int,
        metrics: Dict[str, Any],
        prefix: str = "",
    ) -> str:
        if not filename:
            # filename is not set, use default name
            filename = "{epoch}"
        # check and parse user passed keys in the string
        groups = re.findall(r"(\{.*?)[:\}]", filename)
        if groups:
            metrics["epoch"] = epoch
            for group in groups:
                name = group[1:]
                filename = filename.replace(group, name + "={" + name)
                if name not in metrics:
                    metrics[name] = 0
            filename = filename.format(**metrics)
        return cls.CHECKPOINT_JOIN_CHAR.join([txt for txt in (prefix, filename) if txt])

    def format_checkpoint_name(
        self, epoch: int, metrics: Dict[str, Any], ver: Optional[int] = None
    ) -> str:
        """Generate a filename according to the defined template.

        Example::

            >>> tmpdir = os.path.dirname(__file__)
            >>> ckpt = ModelCheckpoint(os.path.join(tmpdir, '{epoch}'))
            >>> os.path.basename(ckpt.format_checkpoint_name(0, {}))
            'epoch=0.ckpt'
            >>> ckpt = ModelCheckpoint(os.path.join(tmpdir, '{epoch:03d}'))
            >>> os.path.basename(ckpt.format_checkpoint_name(5, {}))
            'epoch=005.ckpt'
            >>> ckpt = ModelCheckpoint(os.path.join(tmpdir, '{epoch}-{val_loss:.2f}'))
            >>> os.path.basename(ckpt.format_checkpoint_name(2, dict(val_loss=0.123456)))
            'epoch=2-val_loss=0.12.ckpt'
            >>> ckpt = ModelCheckpoint(os.path.join(tmpdir, '{missing:d}'))
            >>> os.path.basename(ckpt.format_checkpoint_name(0, {}))
            'missing=0.ckpt'
        """
        filename = self._format_checkpoint_name(
            self.filename, epoch, metrics, prefix=self.prefix
        )
        if ver is not None:
            filename = self.CHECKPOINT_JOIN_CHAR.join((filename, f"v{ver}"))
        ckpt_name = f"{filename}.ckpt"
        return os.path.join(self.dirpath, ckpt_name) if self.dirpath else ckpt_name

    @rank_zero_only
    def __resolve_ckpt_dir(self, trainer, pl_module):
        """
        Determines model checkpoint save directory at runtime. References attributes from the
        trainer's logger to determine where to save checkpoints.
        The base path for saving weights is set in this priority:

        1.  Checkpoint callback's path (if passed in)
        2.  The default_root_dir from trainer if trainer has no logger
        3.  The weights_save_path from trainer, if user provides it
        4.  User provided weights_saved_path

        The base path gets extended with logger name and version (if these are available)
        and subfolder "checkpoints".
        """
        if self.dirpath is not None:
            return  # short circuit

        self.filename = None

        if trainer.logger is not None:
            if trainer.weights_save_path != trainer.default_root_dir:
                # the user has changed weights_save_path, it overrides anything
                save_dir = trainer.weights_save_path
            else:
                save_dir = trainer.logger.save_dir or trainer.default_root_dir

            version = (
                trainer.logger.version
                if isinstance(trainer.logger.version, str)
                else f"version_{trainer.logger.version}"
            )
            ckpt_path = os.path.join(
                save_dir, trainer.logger.name, version, "checkpoints"
            )
        else:
            ckpt_path = os.path.join(trainer.weights_save_path, "checkpoints")

        self.dirpath = ckpt_path

        assert (
            trainer.global_rank == 0
        ), "tried to make a checkpoint from non global_rank=0"
        self._fs.makedirs(self.dirpath, exist_ok=True)

    def _add_backward_monitor_support(self, trainer):
        metrics = trainer.logger_connector.callback_metrics

        # backward compatibility... need to deprecate
        if self.monitor is None and 'val_loss' in metrics:
            self.monitor = 'val_loss'

        if self.monitor is None and 'checkpoint_on' in metrics:
            self.monitor = 'checkpoint_on'

    def _validate_monitor_key(self, trainer):
        metrics = trainer.logger_connector.callback_metrics

        # validate metric
        if self.monitor is not None and not self._is_valid_monitor_key(metrics):
            m = (
                f"ModelCheckpoint(monitor='{self.monitor}') not found in the returned metrics:"
                f" {list(metrics.keys())}. "
                f"HINT: Did you call self.log('{self.monitor}', tensor) in the LightningModule?"
            )
            raise MisconfigurationException(m)

    def _should_skip_epoch(self, trainer):
        epoch = trainer.current_epoch
        return (self.epoch_last_check is not None) and (epoch - self.epoch_last_check) < self.period

    def _get_metric_interpolated_filepath_name(self, epoch, ckpt_name_metrics):
        filepath = self.format_checkpoint_name(epoch, ckpt_name_metrics)
        version_cnt = 0
        while self._fs.exists(filepath):
            filepath = self.format_checkpoint_name(
                epoch, ckpt_name_metrics, ver=version_cnt
            )
            # this epoch called before
            version_cnt += 1
        return filepath

    def _monitor_candidates(self, trainer):
        ckpt_name_metrics = deepcopy(trainer.logger_connector.logged_metrics)
        ckpt_name_metrics.update(trainer.logger_connector.callback_metrics)
        ckpt_name_metrics.update(trainer.logger_connector.progress_bar_metrics)
        return ckpt_name_metrics

    def _save_last_checkpoint(self, trainer, pl_module, epoch, ckpt_name_metrics, filepath):
        should_save_last = self.monitor is None or self.save_last
        if not should_save_last:
            return

        last_filepath = filepath

        # when user ALSO asked for the 'last.ckpt' change the name
        if self.save_last:
            filename = self._format_checkpoint_name(
                self.CHECKPOINT_NAME_LAST, epoch, ckpt_name_metrics, prefix=self.prefix
            )
            last_filepath = os.path.join(self.dirpath, f"{filename}.ckpt")

        self._save_model(last_filepath, trainer, pl_module)
        if self.last_model_path and self.last_model_path != last_filepath:
            self._del_model(self.last_model_path)
        self.last_model_path = last_filepath

        if self.monitor is None:
            self.best_model_path = self.last_model_path

    def _save_top_k_checkpoints(self, metrics, trainer, pl_module, epoch, filepath):
        current = metrics.get(self.monitor)

        if not isinstance(current, torch.Tensor) and current is not None:
            if current is not None:
                current = torch.tensor(current).to(pl_module.device)

        if current is None:
            m = f"Can save best model only with {self.monitor} available, skipping."
            if self.monitor == 'checkpoint_on':
                m = f'No checkpoint_on found. Hint: Did you set it in EvalResult(checkpoint_on=tensor) or ' \
                    f'TrainResult(checkpoint_on=tensor)?'
            rank_zero_warn(m, RuntimeWarning)
        elif self.check_monitor_top_k(current):
            self._do_check_save(filepath, current, epoch, trainer, pl_module)
        elif self.verbose:
            log.info(
                f"Epoch {epoch:d}: {self.monitor} was not in top {self.save_top_k}"
            )

    def _save_all_checkpoints(self, trainer, pl_module, epoch, filepath):
        if self.verbose:
            log.info(f"Epoch {epoch:d}: saving model to {filepath}")

        assert (trainer.global_rank == 0), "tried to make a checkpoint from non global_rank=0"
        self._save_model(filepath, trainer, pl_module)

    def _is_valid_monitor_key(self, metrics):
        return self.monitor in metrics or len(metrics) == 0

    def _do_check_save(
        self,
        filepath: str,
        current: torch.Tensor,
        epoch: int,
        trainer,
        pl_module,
    ):
        # remove kth

        del_list = []
        if len(self.best_k_models) == self.save_top_k and self.save_top_k > 0:
            delpath = self.kth_best_model_path
            self.best_k_models.pop(self.kth_best_model_path)
            del_list.append(delpath)

        self.best_k_models[filepath] = current
        if len(self.best_k_models) == self.save_top_k:
            # monitor dict has reached k elements
            _op = max if self.mode == "min" else min
            self.kth_best_model_path = _op(
                self.best_k_models, key=self.best_k_models.get
            )
            self.kth_value = self.best_k_models[self.kth_best_model_path]

        _op = min if self.mode == "min" else max
        self.best_model_path = _op(self.best_k_models, key=self.best_k_models.get)
        self.best_model_score = self.best_k_models[self.best_model_path]

        if self.verbose:
            log.info(
                f"Epoch {epoch:d}: {self.monitor} reached"
                f" {current:0.5f} (best {self.best_model_score:0.5f}),"
                f" saving model to {filepath} as top {self.save_top_k}"
            )
        self._save_model(filepath, trainer, pl_module)

        for cur_path in del_list:
            if cur_path != filepath:
                self._del_model(cur_path)

