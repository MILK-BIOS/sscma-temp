# Copyright (c) OpenMMLab. All rights reserved.
from itertools import product
from typing import List, Optional, Sequence, Union

import mmengine
import numpy as np
import torch
import torch.nn.functional as F
from mmengine.evaluator import BaseMetric

from sscma.registry import METRICS


def to_tensor(value):
    """Convert value to torch.Tensor."""
    if isinstance(value, np.ndarray):
        value = torch.from_numpy(value)
    elif isinstance(value, Sequence) and not mmengine.is_str(value):
        value = torch.tensor(value)
    elif not isinstance(value, torch.Tensor):
        raise TypeError(f'{type(value)} is not an available argument.')
    return value


def _precision_recall_f1_support(pred_positive, gt_positive, average):
    """calculate base classification task metrics, such as  precision, recall,
    f1_score, support."""
    average_options = ['micro', 'macro', None]
    assert average in average_options, 'Invalid `average` argument, ' \
        f'please specify from {average_options}.'

    # ignore -1 target such as difficult sample that is not wanted
    # in evaluation results.
    # only for calculate multi-label without affecting single-label behavior
    ignored_index = gt_positive == -1
    pred_positive[ignored_index] = 0
    gt_positive[ignored_index] = 0

    class_correct = (pred_positive & gt_positive)
    if average == 'micro':
        tp_sum = class_correct.sum()
        pred_sum = pred_positive.sum()
        gt_sum = gt_positive.sum()
    else:
        tp_sum = class_correct.sum(0)
        pred_sum = pred_positive.sum(0)
        gt_sum = gt_positive.sum(0)

    precision = tp_sum / torch.clamp(pred_sum, min=1).float() * 100
    recall = tp_sum / torch.clamp(gt_sum, min=1).float() * 100
    f1_score = 2 * precision * recall / torch.clamp(
        precision + recall, min=torch.finfo(torch.float32).eps)
    if average in ['macro', 'micro']:
        precision = precision.mean(0)
        recall = recall.mean(0)
        f1_score = f1_score.mean(0)
        support = gt_sum.sum(0)
    else:
        support = gt_sum
    return precision, recall, f1_score, support


def _generate_candidate_indices(ann_file: str = None) -> Optional[list]:
    """generate index candidates for ImageNet-A, ImageNet-R, ImageNet-S.

    Args:
        ann_file (str, optional): The path of the annotation file. This
            file will be used in evaluating the fine-tuned model on OOD
            dataset, e.g. ImageNet-A. Defaults to None.

    Returns:
        Optional[list]: index candidates for ImageNet-A, ImageNet-R, ImageNet-S
    """
    if ann_file is not None:
        with open(ann_file, 'r') as f:
            labels = [int(item.strip().split()[-1]) for item in f.readlines()]
        label_dict = {label: 1 for label in labels}
        return list(label_dict.keys())
    else:
        return None


@METRICS.register_module()
class Accuracy(BaseMetric):
    r"""Accuracy evaluation metric.

    For either binary classification or multi-class classification, the
    accuracy is the fraction of correct predictions in all predictions:

    .. math::

        \text{Accuracy} = \frac{N_{\text{correct}}}{N_{\text{all}}}

    Args:
        topk (int | Sequence[int]): If the ground truth label matches one of
            the best **k** predictions, the sample will be regard as a positive
            prediction. If the parameter is a tuple, all of top-k accuracy will
            be calculated and outputted together. Defaults to 1.
        thrs (Sequence[float | None] | float | None): If a float, predictions
            with score lower than the threshold will be regard as the negative
            prediction. If None, not apply threshold. If the parameter is a
            tuple, accuracy based on all thresholds will be calculated and
            outputted together. Defaults to 0.
        collect_device (str): Device name used for collecting results from
            different ranks during distributed training. Must be 'cpu' or
            'gpu'. Defaults to 'cpu'.
        prefix (str, optional): The prefix that will be added in the metric
            names to disambiguate homonymous metrics of different evaluators.
            If prefix is not provided in the argument, self.default_prefix
            will be used instead. Defaults to None.
        ann_file (str, optional): The path of the annotation file. This
            file will be used in evaluating the fine-tuned model on OOD
            dataset, e.g. ImageNet-A. Defaults to None.

    """
    default_prefix: Optional[str] = 'accuracy'

    def __init__(self,
                 topk: Union[int, Sequence[int]] = (1, ),
                 thrs: Union[float, Sequence[Union[float, None]], None] = 0.,
                 collect_device: str = 'cpu',
                 prefix: Optional[str] = None,
                 ann_file: Optional[str] = None) -> None:
        super().__init__(collect_device=collect_device, prefix=prefix)

        if isinstance(topk, int):
            self.topk = (topk, )
        else:
            self.topk = tuple(topk)

        if isinstance(thrs, float) or thrs is None:
            self.thrs = (thrs, )
        else:
            self.thrs = tuple(thrs)

        # generate index candidates for ImageNet-A, ImageNet-R, ImageNet-S
        self.index_candidates = _generate_candidate_indices(ann_file)

    def process(self, data_batch, data_samples: Sequence[dict]):
        """Process one batch of data samples.

        The processed results should be stored in ``self.results``, which will
        be used to computed the metrics when all batches have been processed.

        Args:
            data_batch: A batch of data from the dataloader.
            data_samples (Sequence[dict]): A batch of outputs from the model.
        """

        for data_sample in data_samples:
            result = dict()
            pred_label = data_sample['pred_label']
            gt_label = data_sample['gt_label']
            if 'score' in pred_label:
                if self.index_candidates is not None:
                    pred_label['score'] = pred_label['score'].cpu()
                    # Since we only compute the topk across the candidate
                    # indices, we need to add 1 to the score of the candidates
                    # to ensure that the candidates are in the topk.
                    pred_label['score'][
                        ..., self.index_candidates] = pred_label['score'][
                            ..., self.index_candidates] + 1.0
                result['pred_score'] = pred_label['score'].cpu()
            else:
                result['pred_label'] = pred_label['label'].cpu()
            result['gt_label'] = gt_label['label'].cpu()
            # Save the result to `self.results`.
            self.results.append(result)

    def compute_metrics(self, results: List):
        """Compute the metrics from processed results.

        Args:
            results (dict): The processed results of each batch.

        Returns:
            Dict: The computed metrics. The keys are the names of the metrics,
            and the values are corresponding results.
        """
        # NOTICE: don't access `self.results` from the method.
        metrics = {}

        # concat
        target = torch.cat([res['gt_label'] for res in results])
        if 'pred_score' in results[0]:
            pred = torch.stack([res['pred_score'] for res in results])

            try:
                acc = self.calculate(pred, target, self.topk, self.thrs)
            except ValueError as e:
                # If the topk is invalid.
                raise ValueError(
                    str(e) + ' Please check the `val_evaluator` and '
                    '`test_evaluator` fields in your config file.')

            multi_thrs = len(self.thrs) > 1
            for i, k in enumerate(self.topk):
                for j, thr in enumerate(self.thrs):
                    name = f'top{k}'
                    if multi_thrs:
                        name += '_no-thr' if thr is None else f'_thr-{thr:.2f}'
                    metrics[name] = acc[i][j].item()
        else:
            # If only label in the `pred_label`.
            pred = torch.cat([res['pred_label'] for res in results])
            acc = self.calculate(pred, target, self.topk, self.thrs)
            metrics['top1'] = acc.item()

        return metrics

    @staticmethod
    def calculate(
        pred: Union[torch.Tensor, np.ndarray, Sequence],
        target: Union[torch.Tensor, np.ndarray, Sequence],
        topk: Sequence[int] = (1, ),
        thrs: Sequence[Union[float, None]] = (0., ),
    ) -> Union[torch.Tensor, List[List[torch.Tensor]]]:
        """Calculate the accuracy.

        Args:
            pred (torch.Tensor | np.ndarray | Sequence): The prediction
                results. It can be labels (N, ), or scores of every
                class (N, C).
            target (torch.Tensor | np.ndarray | Sequence): The target of
                each prediction with shape (N, ).
            thrs (Sequence[float | None]): Predictions with scores under
                the thresholds are considered negative. It's only used
                when ``pred`` is scores. None means no thresholds.
                Defaults to (0., ).
            thrs (Sequence[float]): Predictions with scores under
                the thresholds are considered negative. It's only used
                when ``pred`` is scores. Defaults to (0., ).

        Returns:
            torch.Tensor | List[List[torch.Tensor]]: Accuracy.

            - torch.Tensor: If the ``pred`` is a sequence of label instead of
              score (number of dimensions is 1). Only return a top-1 accuracy
              tensor, and ignore the argument ``topk` and ``thrs``.
            - List[List[torch.Tensor]]: If the ``pred`` is a sequence of score
              (number of dimensions is 2). Return the accuracy on each ``topk``
              and ``thrs``. And the first dim is ``topk``, the second dim is
              ``thrs``.
        """

        pred = to_tensor(pred)
        target = to_tensor(target).to(torch.int64)
        num = pred.size(0)
        assert pred.size(0) == target.size(0), \
            f"The size of pred ({pred.size(0)}) doesn't match "\
            f'the target ({target.size(0)}).'

        if pred.ndim == 1:
            # For pred label, ignore topk and acc
            pred_label = pred.int()
            correct = pred.eq(target).float().sum(0, keepdim=True)
            acc = correct.mul_(100. / num)
            return acc
        else:
            # For pred score, calculate on all topk and thresholds.
            pred = pred.float()
            maxk = max(topk)

            if maxk > pred.size(1):
                raise ValueError(
                    f'Top-{maxk} accuracy is unavailable since the number of '
                    f'categories is {pred.size(1)}.')

            pred_score, pred_label = pred.topk(maxk, dim=1)
            pred_label = pred_label.t()
            correct = pred_label.eq(target.view(1, -1).expand_as(pred_label))
            results = []
            for k in topk:
                results.append([])
                for thr in thrs:
                    # Only prediction values larger than thr are counted
                    # as correct
                    _correct = correct
                    if thr is not None:
                        _correct = _correct & (pred_score.t() > thr)
                    correct_k = _correct[:k].reshape(-1).float().sum(
                        0, keepdim=True)
                    acc = correct_k.mul_(100. / num)
                    results[-1].append(acc)
            return results


@METRICS.register_module()
class SingleLabelMetric(BaseMetric):
    r"""A collection of precision, recall, f1-score and support for
    single-label tasks.

    The collection of metrics is for single-label multi-class classification.
    And all these metrics are based on the confusion matrix of every category:

    .. image:: ../../_static/image/confusion-matrix.png
       :width: 60%
       :align: center

    All metrics can be formulated use variables above:

    **Precision** is the fraction of correct predictions in all predictions:

    .. math::
        \text{Precision} = \frac{TP}{TP+FP}

    **Recall** is the fraction of correct predictions in all targets:

    .. math::
        \text{Recall} = \frac{TP}{TP+FN}

    **F1-score** is the harmonic mean of the precision and recall:

    .. math::
        \text{F1-score} = \frac{2\times\text{Recall}\times\text{Precision}}{\text{Recall}+\text{Precision}}

    **Support** is the number of samples:

    .. math::
        \text{Support} = TP + TN + FN + FP

    Args:
        thrs (Sequence[float | None] | float | None): If a float, predictions
            with score lower than the threshold will be regard as the negative
            prediction. If None, only the top-1 prediction will be regard as
            the positive prediction. If the parameter is a tuple, accuracy
            based on all thresholds will be calculated and outputted together.
            Defaults to 0.
        items (Sequence[str]): The detailed metric items to evaluate, select
            from "precision", "recall", "f1-score" and "support".
            Defaults to ``('precision', 'recall', 'f1-score')``.
        average (str | None): How to calculate the final metrics from the
            confusion matrix of every category. It supports three modes:

            - `"macro"`: Calculate metrics for each category, and calculate
              the mean value over all categories.
            - `"micro"`: Average the confusion matrix over all categories and
              calculate metrics on the mean confusion matrix.
            - `None`: Calculate metrics of every category and output directly.

            Defaults to "macro".
        num_classes (int, optional): The number of classes. Defaults to None.
        collect_device (str): Device name used for collecting results from
            different ranks during distributed training. Must be 'cpu' or
            'gpu'. Defaults to 'cpu'.
        prefix (str, optional): The prefix that will be added in the metric
            names to disambiguate homonymous metrics of different evaluators.
            If prefix is not provided in the argument, self.default_prefix
            will be used instead. Defaults to None.

    
    """  # noqa: E501
    default_prefix: Optional[str] = 'single-label'

    def __init__(self,
                 thrs: Union[float, Sequence[Union[float, None]], None] = 0.,
                 items: Sequence[str] = ('precision', 'recall', 'f1-score'),
                 average: Optional[str] = 'macro',
                 num_classes: Optional[int] = None,
                 collect_device: str = 'cpu',
                 prefix: Optional[str] = None) -> None:
        super().__init__(collect_device=collect_device, prefix=prefix)

        if isinstance(thrs, float) or thrs is None:
            self.thrs = (thrs, )
        else:
            self.thrs = tuple(thrs)

        for item in items:
            assert item in ['precision', 'recall', 'f1-score', 'support'], \
                f'The metric {item} is not supported by `SingleLabelMetric`,' \
                ' please specify from "precision", "recall", "f1-score" and ' \
                '"support".'
        self.items = tuple(items)
        self.average = average
        self.num_classes = num_classes

    def process(self, data_batch, data_samples: Sequence[dict]):
        """Process one batch of data samples.

        The processed results should be stored in ``self.results``, which will
        be used to computed the metrics when all batches have been processed.

        Args:
            data_batch: A batch of data from the dataloader.
            data_samples (Sequence[dict]): A batch of outputs from the model.
        """

        for data_sample in data_samples:
            result = dict()
            pred_label = data_sample['pred_label']
            gt_label = data_sample['gt_label']
            if 'score' in pred_label:
                result['pred_score'] = pred_label['score'].cpu()
            else:
                num_classes = self.num_classes or data_sample.get(
                    'num_classes')
                assert num_classes is not None, \
                    'The `num_classes` must be specified if `pred_label` has '\
                    'only `label`.'
                result['pred_label'] = pred_label['label'].cpu()
                result['num_classes'] = num_classes
            result['gt_label'] = gt_label['label'].cpu()
            # Save the result to `self.results`.
            self.results.append(result)

    def compute_metrics(self, results: List):
        """Compute the metrics from processed results.

        Args:
            results (list): The processed results of each batch.

        Returns:
            Dict: The computed metrics. The keys are the names of the metrics,
            and the values are corresponding results.
        """
        # NOTICE: don't access `self.results` from the method. `self.results`
        # are a list of results from multiple batch, while the input `results`
        # are the collected results.
        metrics = {}

        def pack_results(precision, recall, f1_score, support):
            single_metrics = {}
            if 'precision' in self.items:
                single_metrics['precision'] = precision
            if 'recall' in self.items:
                single_metrics['recall'] = recall
            if 'f1-score' in self.items:
                single_metrics['f1-score'] = f1_score
            if 'support' in self.items:
                single_metrics['support'] = support
            return single_metrics

        # concat
        target = torch.cat([res['gt_label'] for res in results])
        if 'pred_score' in results[0]:
            pred = torch.stack([res['pred_score'] for res in results])
            metrics_list = self.calculate(
                pred, target, thrs=self.thrs, average=self.average)

            multi_thrs = len(self.thrs) > 1
            for i, thr in enumerate(self.thrs):
                if multi_thrs:
                    suffix = '_no-thr' if thr is None else f'_thr-{thr:.2f}'
                else:
                    suffix = ''

                for k, v in pack_results(*metrics_list[i]).items():
                    metrics[k + suffix] = v
        else:
            # If only label in the `pred_label`.
            pred = torch.cat([res['pred_label'] for res in results])
            res = self.calculate(
                pred,
                target,
                average=self.average,
                num_classes=results[0]['num_classes'])
            metrics = pack_results(*res)

        result_metrics = dict()
        for k, v in metrics.items():

            if self.average is None:
                result_metrics[k + '_classwise'] = v.cpu().detach().tolist()
            elif self.average == 'micro':
                result_metrics[k + f'_{self.average}'] = v.item()
            else:
                result_metrics[k] = v.item()

        return result_metrics

    @staticmethod
    def calculate(
        pred: Union[torch.Tensor, np.ndarray, Sequence],
        target: Union[torch.Tensor, np.ndarray, Sequence],
        thrs: Sequence[Union[float, None]] = (0., ),
        average: Optional[str] = 'macro',
        num_classes: Optional[int] = None,
    ) -> Union[torch.Tensor, List[torch.Tensor]]:
        """Calculate the precision, recall, f1-score and support.

        Args:
            pred (torch.Tensor | np.ndarray | Sequence): The prediction
                results. It can be labels (N, ), or scores of every
                class (N, C).
            target (torch.Tensor | np.ndarray | Sequence): The target of
                each prediction with shape (N, ).
            thrs (Sequence[float | None]): Predictions with scores under
                the thresholds are considered negative. It's only used
                when ``pred`` is scores. None means no thresholds.
                Defaults to (0., ).
            average (str | None): How to calculate the final metrics from
                the confusion matrix of every category. It supports three
                modes:

                - `"macro"`: Calculate metrics for each category, and calculate
                  the mean value over all categories.
                - `"micro"`: Average the confusion matrix over all categories
                  and calculate metrics on the mean confusion matrix.
                - `None`: Calculate metrics of every category and output
                  directly.

                Defaults to "macro".
            num_classes (Optional, int): The number of classes. If the ``pred``
                is label instead of scores, this argument is required.
                Defaults to None.

        Returns:
            Tuple: The tuple contains precision, recall and f1-score.
            And the type of each item is:

            - torch.Tensor: If the ``pred`` is a sequence of label instead of
              score (number of dimensions is 1). Only returns a tensor for
              each metric. The shape is (1, ) if ``classwise`` is False, and
              (C, ) if ``classwise`` is True.
            - List[torch.Tensor]: If the ``pred`` is a sequence of score
              (number of dimensions is 2). Return the metrics on each ``thrs``.
              The shape of tensor is (1, ) if ``classwise`` is False, and (C, )
              if ``classwise`` is True.
        """
        average_options = ['micro', 'macro', None]
        assert average in average_options, 'Invalid `average` argument, ' \
            f'please specify from {average_options}.'

        pred = to_tensor(pred)
        target = to_tensor(target).to(torch.int64)
        assert pred.size(0) == target.size(0), \
            f"The size of pred ({pred.size(0)}) doesn't match "\
            f'the target ({target.size(0)}).'

        if pred.ndim == 1:
            assert num_classes is not None, \
                'Please specify the `num_classes` if the `pred` is labels ' \
                'intead of scores.'
            gt_positive = F.one_hot(target.flatten(), num_classes)
            pred_positive = F.one_hot(pred.to(torch.int64), num_classes)
            return _precision_recall_f1_support(pred_positive, gt_positive,
                                                average)
        else:
            # For pred score, calculate on all thresholds.
            num_classes = pred.size(1)
            pred_score, pred_label = torch.topk(pred, k=1)
            pred_score = pred_score.flatten()
            pred_label = pred_label.flatten()

            gt_positive = F.one_hot(target.flatten(), num_classes)

            results = []
            for thr in thrs:
                pred_positive = F.one_hot(pred_label, num_classes)
                if thr is not None:
                    pred_positive[pred_score <= thr] = 0
                results.append(
                    _precision_recall_f1_support(pred_positive, gt_positive,
                                                 average))

            return results


@METRICS.register_module()
class ConfusionMatrix(BaseMetric):
    r"""A metric to calculate confusion matrix for single-label tasks.

    Args:
        num_classes (int, optional): The number of classes. Defaults to None.
        collect_device (str): Device name used for collecting results from
            different ranks during distributed training. Must be 'cpu' or
            'gpu'. Defaults to 'cpu'.
        prefix (str, optional): The prefix that will be added in the metric
            names to disambiguate homonymous metrics of different evaluators.
            If prefix is not provided in the argument, self.default_prefix
            will be used instead. Defaults to None.

    """  # noqa: E501
    default_prefix = 'confusion_matrix'

    def __init__(self,
                 num_classes: Optional[int] = None,
                 collect_device: str = 'cpu',
                 prefix: Optional[str] = None) -> None:
        super().__init__(collect_device, prefix)

        self.num_classes = num_classes

    def process(self, data_batch, data_samples: Sequence[dict]) -> None:
        for data_sample in data_samples:
            pred = data_sample['pred_label']
            gt_label = data_sample['gt_label']['label']
            if 'score' in pred:
                pred_label = pred['score'].argmax(dim=0, keepdim=True)
                self.num_classes = pred['score'].size(0)
            else:
                pred_label = pred['label']

            self.results.append({
                'pred_label': pred_label,
                'gt_label': gt_label
            })

    def compute_metrics(self, results: list) -> dict:
        pred_labels = []
        gt_labels = []
        for result in results:
            pred_labels.append(result['pred_label'])
            gt_labels.append(result['gt_label'])
        confusion_matrix = ConfusionMatrix.calculate(
            torch.cat(pred_labels),
            torch.cat(gt_labels),
            num_classes=self.num_classes)
        return {'result': confusion_matrix}

    @staticmethod
    def calculate(pred, target, num_classes=None) -> dict:
        """Calculate the confusion matrix for single-label task.

        Args:
            pred (torch.Tensor | np.ndarray | Sequence): The prediction
                results. It can be labels (N, ), or scores of every
                class (N, C).
            target (torch.Tensor | np.ndarray | Sequence): The target of
                each prediction with shape (N, ).
            num_classes (Optional, int): The number of classes. If the ``pred``
                is label instead of scores, this argument is required.
                Defaults to None.

        Returns:
            torch.Tensor: The confusion matrix.
        """
        pred = to_tensor(pred)
        target_label = to_tensor(target).int()

        assert pred.size(0) == target_label.size(0), \
            f"The size of pred ({pred.size(0)}) doesn't match "\
            f'the target ({target_label.size(0)}).'
        assert target_label.ndim == 1

        if pred.ndim == 1:
            assert num_classes is not None, \
                'Please specify the `num_classes` if the `pred` is labels ' \
                'intead of scores.'
            pred_label = pred
        else:
            num_classes = num_classes or pred.size(1)
            pred_label = torch.argmax(pred, dim=1).flatten()

        with torch.no_grad():
            indices = num_classes * target_label + pred_label
            matrix = torch.bincount(indices, minlength=num_classes**2)
            matrix = matrix.reshape(num_classes, num_classes)

        return matrix

    @staticmethod
    def plot(confusion_matrix: torch.Tensor,
             include_values: bool = False,
             cmap: str = 'viridis',
             classes: Optional[List[str]] = None,
             colorbar: bool = True,
             show: bool = True):
        """Draw a confusion matrix by matplotlib.

        Modified from `Scikit-Learn
        <https://github.com/scikit-learn/scikit-learn/blob/dc580a8ef/sklearn/metrics/_plot/confusion_matrix.py#L81>`_

        Args:
            confusion_matrix (torch.Tensor): The confusion matrix to draw.
            include_values (bool): Whether to draw the values in the figure.
                Defaults to False.
            cmap (str): The color map to use. Defaults to use "viridis".
            classes (list[str], optional): The names of categories.
                Defaults to None, which means to use index number.
            colorbar (bool): Whether to show the colorbar. Defaults to True.
            show (bool): Whether to show the figure immediately.
                Defaults to True.
        """  # noqa: E501
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(10, 10))

        num_classes = confusion_matrix.size(0)

        im_ = ax.imshow(confusion_matrix, interpolation='nearest', cmap=cmap)
        text_ = None
        cmap_min, cmap_max = im_.cmap(0), im_.cmap(1.0)

        if include_values:
            text_ = np.empty_like(confusion_matrix, dtype=object)

            # print text with appropriate color depending on background
            thresh = (confusion_matrix.max() + confusion_matrix.min()) / 2.0

            for i, j in product(range(num_classes), range(num_classes)):
                color = cmap_max if confusion_matrix[i,
                                                     j] < thresh else cmap_min

                text_cm = format(confusion_matrix[i, j], '.2g')
                text_d = format(confusion_matrix[i, j], 'd')
                if len(text_d) < len(text_cm):
                    text_cm = text_d

                text_[i, j] = ax.text(
                    j, i, text_cm, ha='center', va='center', color=color)

        display_labels = classes or np.arange(num_classes)

        if colorbar:
            fig.colorbar(im_, ax=ax)
        ax.set(
            xticks=np.arange(num_classes),
            yticks=np.arange(num_classes),
            xticklabels=display_labels,
            yticklabels=display_labels,
            ylabel='True label',
            xlabel='Predicted label',
        )
        ax.invert_yaxis()
        ax.xaxis.tick_top()

        ax.set_ylim((num_classes - 0.5, -0.5))
        # Automatically rotate the x labels.
        fig.autofmt_xdate(ha='center')

        if show:
            plt.show()
        return fig
