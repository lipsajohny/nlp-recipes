# coding=utf-8
# Copyright (c) Microsoft. All rights reserved.
import json
import logging
import os
import random
from datetime import datetime

import torch
from tensorboardX import SummaryWriter
from torch.utils.data import BatchSampler, DataLoader, Dataset

from utils_nlp.models.mtdnn.common.glue.glue_utils import submit
from utils_nlp.models.mtdnn.common.types import TaskType
from utils_nlp.models.mtdnn.common.utils import MTDNNCommonUtils
from utils_nlp.models.mtdnn.configuration_mtdnn import MTDNNConfig
from utils_nlp.models.mtdnn.dataset_mtdnn import (
    MTDNNCollater,
    MTDNNMultiTaskBatchSampler,
    MTDNNMultiTaskDataset,
    MTDNNSingleTaskDataset,
)
from utils_nlp.models.mtdnn.modeling_mtdnn import MTDNNModel
from utils_nlp.models.mtdnn.tasks.config import TaskDefs

logger = logging.getLogger(__name__)


class MTDNNDataProcess:
    def __init__(
        self,
        config: MTDNNConfig,
        task_defs: TaskDefs,
        batch_size: int,
        data_dir: str = "data/canonical_data/bert_uncased_lower",
        train_datasets_list: list = ["mnli"],
        test_datasets_list: list = ["mnli_mismatched,mnli_matched"],
        glue_format: bool = False,
        data_sort: bool = False,
    ):
        assert len(train_datasets_list) >= 1, "Train dataset list cannot be empty"
        assert len(test_datasets_list) >= 1, "Test dataset list cannot be empty"

        # Initialize class members
        self.config = config
        self.task_defs = task_defs
        self.train_datasets = train_datasets_list
        self.test_datasets = test_datasets_list
        self.data_dir = data_dir
        self.glue_format = glue_format
        self.data_sort = data_sort
        self.batch_size = batch_size
        self.tasks = {}
        self.tasks_class = {}
        self.nclass_list = []
        self.decoder_opts = []
        self.task_types = []
        self.dropout_list = []
        self.loss_types = []
        self.kd_loss_types = []
        self._multitask_train_dataloader = self._process_train_datasets()
        self._dev_dataloaders_list, self._test_dataloaders_list = self._process_dev_test_datasets()
        self._num_all_batches = (
            self.config.epochs
            * len(self._multitask_train_dataloader)
            // self.config.grad_accumulation_step
        )

    def _process_train_datasets(self):
        """Preprocess the training sets and generate decoding and task specific training options needed to update config object
        
        Returns:
            [DataLoader] -- Multiple tasks train data ready for training
        """
        logger.info("Starting to process the training data sets")

        train_datasets = []
        for dataset in self.train_datasets:
            prefix = dataset.split("_")[0]
            if prefix in self.tasks:
                continue
            assert prefix in self.task_defs.n_class_map
            assert prefix in self.task_defs.data_type_map
            data_type = self.task_defs.data_type_map[prefix]
            nclass = self.task_defs.n_class_map[prefix]
            task_id = len(self.tasks)
            if self.config.mtl_opt > 0:
                task_id = (
                    self.tasks_class[nclass]
                    if nclass in self.tasks_class
                    else len(self.tasks_class)
                )

            task_type = self.task_defs.task_type_map[prefix]

            dopt = self.generate_decoder_opt(
                self.task_defs.enable_san_map[prefix], self.config.answer_opt
            )
            if task_id < len(self.decoder_opts):
                self.decoder_opts[task_id] = min(self.decoder_opts[task_id], dopt)
            else:
                self.decoder_opts.append(dopt)
            self.task_types.append(task_type)
            self.loss_types.append(self.task_defs.loss_map[prefix])
            self.kd_loss_types.append(self.task_defs.kd_loss_map[prefix])

            if prefix not in self.tasks:
                self.tasks[prefix] = len(self.tasks)
                if self.config.mtl_opt < 1:
                    self.nclass_list.append(nclass)

            if nclass not in self.tasks_class:
                self.tasks_class[nclass] = len(self.tasks_class)
                if self.config.mtl_opt > 0:
                    self.nclass_list.append(nclass)

            dropout_p = self.task_defs.dropout_p_map.get(prefix, self.config.dropout_p)
            self.dropout_list.append(dropout_p)

            train_path = os.path.join(self.data_dir, f"{dataset}_train.json")
            logger.info(f"Loading {train_path} as task {task_id}")
            train_data_set = MTDNNSingleTaskDataset(
                train_path,
                True,
                maxlen=self.config.max_seq_len,
                task_id=task_id,
                task_type=task_type,
                data_type=data_type,
            )
            train_datasets.append(train_data_set)
        train_collater = MTDNNCollater(
            dropout_w=self.config.dropout_w, encoder_type=self.config.encoder_type
        )
        multitask_train_dataset = MTDNNMultiTaskDataset(train_datasets)
        multitask_batch_sampler = MTDNNMultiTaskBatchSampler(
            train_datasets, self.config.batch_size, self.config.mix_opt, self.config.ratio
        )
        multitask_train_data = DataLoader(
            multitask_train_dataset,
            batch_sampler=multitask_batch_sampler,
            collate_fn=train_collater.collate_fn,
            pin_memory=self.config.cuda,
        )

        # Update class configuration with decoder opts
        self.config = self.update_config(self.config)

        return multitask_train_data

    def _process_dev_test_datasets(self):
        """Preprocess the test sets 
        
        Returns:
            [List] -- Multiple tasks test data ready for inference
        """
        logger.info("Starting to process the testing data sets")
        dev_dataloaders_list = []
        test_dataloaders_list = []
        test_collater = MTDNNCollater(is_train=False, encoder_type=self.config.encoder_type)
        for dataset in self.test_datasets:
            prefix = dataset.split("_")[0]
            task_id = (
                self.tasks_class[self.task_defs.n_class_map[prefix]]
                if self.config.mtl_opt > 0
                else self.tasks[prefix]
            )
            task_type = self.task_defs.task_type_map[prefix]

            pw_task = False
            if task_type == TaskType.Ranking:
                pw_task = True

            assert prefix in self.task_defs.data_type_map
            data_type = self.task_defs.data_type_map[prefix]

            dev_path = os.path.join(self.data_dir, f"{dataset}_dev.json")
            dev_data = None
            if os.path.exists(dev_path):
                dev_data_set = MTDNNSingleTaskDataset(
                    dev_path,
                    False,
                    maxlen=self.config.max_seq_len,
                    task_id=task_id,
                    task_type=task_type,
                    data_type=data_type,
                )
                dev_data = DataLoader(
                    dev_data_set,
                    batch_size=self.config.batch_size_eval,
                    collate_fn=test_collater.collate_fn,
                    pin_memory=self.config.cuda,
                )
            dev_dataloaders_list.append(dev_data)

            test_path = os.path.join(self.data_dir, f"{dataset}_test.json")
            test_data = None
            if os.path.exists(test_path):
                test_data_set = MTDNNSingleTaskDataset(
                    test_path,
                    False,
                    maxlen=self.config.max_seq_len,
                    task_id=task_id,
                    task_type=task_type,
                    data_type=data_type,
                )
                test_data = DataLoader(
                    test_data_set,
                    batch_size=self.config.batch_size_eval,
                    collate_fn=test_collater.collate_fn,
                    pin_memory=self.config.cuda,
                )
            test_dataloaders_list.append(test_data)

        # Return tuple of dev and test dataloaders
        return dev_dataloaders_list, test_dataloaders_list

    def get_train_dataloader(self) -> DataLoader:
        """Returns a dataloader for mutliple tasks
        
        Returns:
            DataLoader -- Multiple tasks batch dataloader
        """
        return self._multitask_train_dataloader

    def get_dev_dataloaders(self) -> list:
        """Returns a list of dev dataloaders for multiple tasks
        
        Returns:
            list -- List of dev dataloaders
        """
        return self._dev_dataloaders_list

    def get_test_dataloaders(self) -> list:
        """Returns a list of test dataloaders for multiple tasks
        
        Returns:
            list -- List of test dataloaders
        """
        return self._test_dataloaders_list

    def generate_decoder_opt(self, enable_san, max_opt):
        return max_opt if enable_san and max_opt < 3 else 0

    def update_config(self, config: MTDNNConfig):
        # Update configurations with options obtained from preprocessing training data
        setattr(config, "decoder_opts", self.decoder_opts)
        setattr(config, "task_types", self.task_types)
        setattr(config, "tasks_dropout_p", self.dropout_list)
        setattr(config, "loss_types", self.loss_types)
        setattr(config, "kd_loss_types", self.kd_loss_types)
        setattr(config, "tasks_nclass_list", self.nclass_list)
        return config


class MTDNNPipelineProcess:
    def __init__(
        self,
        model: MTDNNModel,
        config: MTDNNConfig,
        task_defs: TaskDefs,
        multitask_train_dataloader: DataLoader,
        dev_dataloaders_list: list,  # list of dataloaders
        test_dataloaders_list: list,  # list of dataloaders
        test_datasets_list: list = ["mnli_mismatched", "mnli_matched"],
        output_dir: str = "checkpoint",
        log_dir: str = "tensorboard_logdir",
    ):
        """Pipeline process for MTDNN Training, Inference and Fine Tuning
        """
        assert multitask_train_dataloader, "DataLoader for multiple tasks cannot be None"
        assert test_datasets_list, "Pass a list of test dataset prefixes"
        self.model = model
        self.config = config
        self.task_defs = task_defs
        self.multitask_train_dataloader = multitask_train_dataloader
        self.dev_dataloaders_list = dev_dataloaders_list
        self.test_dataloaders_list = test_dataloaders_list
        self.test_datasets_list = test_datasets_list
        self.output_dir = output_dir
        self.log_dir = log_dir
        self.tensor_board = SummaryWriter(log_dir=self.log_dir)

    def fit(self, epochs=0):
        """ Fit model to training datasets """
        epochs = epochs or self.config.epochs
        logger.info(f"Total number of params: {self.model.total_param}")
        for epoch in range(epochs):
            logger.info(f"At epoch {epoch}")
            start = datetime.now()

            print(f"Length to go over: {len(self.multitask_train_dataloader)}")
            # Create batches and train
            for idx, (batch_meta, batch_data) in enumerate(self.multitask_train_dataloader):
                # print(f"Inside training loop idx: {idx} - Meta: {batch_meta} - Data: {batch_data}")
                logger.info(
                    f"Before calling patch_data\n Training batch: {idx}. \t\tBatch Metadata: {batch_meta}. \t\tBatch Data: {batch_data}"
                )
                batch_meta, batch_data = MTDNNCollater.patch_data(
                    self.config.cuda, batch_meta, batch_data
                )
                logger.info(
                    f"After calling patch_data\nTraining batch: {idx}.\t\tBatch Metadata: {batch_meta}. \t\tBatch Data: {batch_data}"
                )
                task_id = batch_meta["task_id"]
                # print(f"Before calling model.update with batch meta: {idx}")
                self.model.update(batch_meta, batch_data)
                # print(f"After calling model.update with batch meta: {idx}")
                if (
                    self.model.local_updates == 1
                    or (self.model.local_updates)
                    % (self.config.log_per_updates * self.config.grad_accumulation_step)
                    == 0
                ):

                    time_left = str(
                        (datetime.now() - start)
                        / (idx + 1)
                        * (len(self.multitask_train_dataloader) - idx - 1)
                    ).split(".")[0]
                    print("Inside time left: ", time_left)
                    logger.info(
                        "Task [{0:2}] updates[{1:6}] train loss[{2:.5f}] remaining[{3}]".format(
                            task_id, self.model.updates, self.model.train_loss.avg, time_left
                        )
                    )
                    if self.config.use_tensor_board:
                        self.tensor_board.add_scalar(
                            "train/loss", self.model.train_loss.avg, global_step=self.model.updates
                        )

                if self.config.save_per_updates_on and (
                    (self.model.local_updates)
                    % (self.config.save_per_updates * self.config.grad_accumulation_step)
                    == 0
                ):
                    model_file = os.path.join(
                        output_dir, "model_{}_{}.pt".format(epoch, self.model.updates)
                    )
                    logger.info(f"Saving mt-dnn model to {model_file}")
                    self.model.save(model_file)

    def predict(self):
        """ Inference of model on test datasets """
        for idx, dataset in enumerate(self.test_datasets_list):
            prefix = dataset.split("_")[0]
            label_dict = self.task_defs.global_map.get(prefix, None)
            dev_data: DataLoader = self.dev_dataloaders_list[idx]
            if dev_data is not None:
                with torch.no_grad():
                    dev_metrics, dev_predictions, scores, golds, dev_ids = self.model.eval_model(
                        dev_data,
                        metric_meta=task_defs.metric_meta_map[prefix],
                        use_cuda=args.cuda,
                        label_mapper=label_dict,
                        task_type=task_defs.task_type_map[prefix],
                    )
                for key, val in dev_metrics.items():
                    if self.config.use_tensor_board:
                        self.tensor_board.add_scalar(f"dev/{dataset}/{key}", val, global_step=epoch)
                    if isinstance(val, str):
                        logger.warning(f"Task {dataset} -- epoch {epoch} -- Dev {key}:\n {val}")
                    else:
                        logger.warning(f"Task {dataset} -- epoch {epoch} -- Dev {key}: {val:.3f}")
                score_file = os.path.join(output_dir, f"{dataset}_dev_scores_{epoch}.json")
                results = {
                    "metrics": dev_metrics,
                    "predictions": dev_predictions,
                    "uids": dev_ids,
                    "scores": scores,
                }

                # Save results to file
                MTDNNCommonUtils.dump(score_file, results)
                if self.config.use_glue_format:
                    official_score_file = os.path.join(
                        output_dir, "{}_dev_scores_{}.tsv".format(dataset, epoch)
                    )
                    submit(official_score_file, results, label_dict)

            # test eval
            test_data = self.test_dataloaders_list[idx]
            if test_data is not None:
                with torch.no_grad():
                    test_metrics, test_predictions, scores, golds, test_ids = self.model.eval_model(
                        test_data,
                        metric_meta=task_defs.metric_meta_map[prefix],
                        use_cuda=args.cuda,
                        with_label=False,
                        label_mapper=label_dict,
                        task_type=task_defs.task_type_map[prefix],
                    )
                score_file = os.path.join(output_dir, f"{dataset}_test_scores_{epoch}.json")
                results = {
                    "metrics": test_metrics,
                    "predictions": test_predictions,
                    "uids": test_ids,
                    "scores": scores,
                }
                MTDNNCommonUtils.dump(score_file, results)
                if args.glue_format_on:
                    official_score_file = os.path.join(
                        output_dir, f"{dataset}_test_scores_{epoch}.tsv"
                    )
                    submit(official_score_file, results, label_dict)
                logger.info("[new test scores saved.]")

        model_file = os.path.join(output_dir, f"model_{epoch}.pt")
        self.model.save(model_file)

        # Close tensorboard connection if opened
        self.close_connections()

    def close_connections(self):
        # Close tensor board connection
        if self.config.use_tensor_board:
            self.tensor_board.close()
