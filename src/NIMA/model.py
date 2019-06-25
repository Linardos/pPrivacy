# -*- coding: utf-8 -*-
# Get weights from the repo as well:
# https://github.com/kentsyx/Neural-IMage-Assessment
import torch
import torch.nn as nn

class NIMA(nn.Module):
    """Neural IMage Assessment model by Google"""
    def __init__(self, base_model, num_classes=10):
        super(NIMA2, self).__init__()
        self.features = base_model.features
        self.classifier = nn.Sequential(
            nn.Dropout(p=0.75),
            # nn.AdaptiveAvgPool2d(25088),
            nn.Linear(in_features=25088, out_features=num_classes),
            nn.Softmax())

    def forward(self, x):
        out = self.features(x)
        out = nn.functional.adaptive_avg_pool2d(out, 7) # 7 matches 25088, this number might need manipulation based on size of images.
        out = out.view(out.size(0), -1)
        out = self.classifier(out)
        return out



def single_emd_loss(p, q, r=2):
    """
    Earth Mover's Distance of one sample

    Args:
        p: true distribution of shape num_classes × 1
        q: estimated distribution of shape num_classes × 1
        r: norm parameter
    """
    assert p.shape == q.shape, "Length of the two distribution must be the same"
    length = p.shape[0]
    emd_loss = 0.0
    for i in range(1, length + 1):
        emd_loss += sum(torch.abs(p[:i] - q[:i])) ** r
    return (emd_loss / length) ** (1. / r)


def emd_loss(p, q, r=2):
    """
    Earth Mover's Distance on a batch

    Args:
        p: true distribution of shape mini_batch_size × num_classes × 1
        q: estimated distribution of shape mini_batch_size × num_classes × 1
        r: norm parameters
    """
    assert p.shape == q.shape, "Shape of the two distribution batches must be the same."
    mini_batch_size = p.shape[0]
    loss_vector = []
    for i in range(mini_batch_size):
        loss_vector.append(single_emd_loss(p[i], q[i], r=r))
    return sum(loss_vector) / mini_batch_size

