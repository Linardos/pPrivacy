# Implementation of the attack. Note that the original code was borrowed from PyTorch tutorials and build upon for the purposes of this project.

from __future__ import print_function
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torchvision import datasets, transforms, models, utils
import numpy as np
import matplotlib.pyplot as plt
import os
import cv2
import datasets
import datetime
from salient_bluring import miniatures
from salient_bluring.saliency_map_generation import infer_smap, SalBCE
from NIMA.model import NIMA, emd_loss
from PIL import Image

torch.manual_seed(10) # original test: 20

ROOT_PATH = "/home/linardos/Documents/pPrivacy"
PATH_TO_DATA = "../data/Places365/val_256"
PATH_TO_LABELS = "../data/Places365/places365_val.txt"
PATH_TO_LABELS = "../data/Places365/MEPP18val.csv"
BATCH_SIZE = 1
USE_MAP = True
TILTSHIFT = True

######################################################################
# Utility functions
# --------------
def minmax_normalization(X):
    return (X-X.min())/(X.max()-X.min())

def rgb2gray(rgb):
    return np.dot(rgb[...,:3], [0.2989, 0.5870, 0.1140])

def load_weights(pt_model, device='cpu'):
    # Load stored model:
    temp = torch.load(pt_model, map_location=device)['state_dict']
    # Because of dataparallel there is contradiction in the name of the keys so we need to remove part of the string in the keys:.
    from collections import OrderedDict
    checkpoint = OrderedDict()
    for key in temp.keys():
        new_key = key.replace("module.","")
        checkpoint[new_key]=temp[key]

    return checkpoint


# ====================

# modules = list(model.children())[:-1] # The last layer is not compatible.
# print(checkpoint['state_dict'].keys())
# print(model.state_dict().keys())


######################################################################
# Implementation
# --------------
#
# Inputs
# ~~~~~~
#
# -  **epsilons** - List of epsilon values to use for the run. It is
#    important to keep 0 in the list because it represents the model
#    performance on the original test set. Also, intuitively we would
#    expect the larger the epsilon, the more noticeable the perturbations
#    but the more effective the attack in terms of degrading model
#    accuracy. Since the data range here is `[0,1]`, no epsilon
#    value should exceed 1.
#


epsilons = [.0, .01, .025, .05] #, .2, .25, .3]
# epsilons = [.15] #, .2, .25, .3]
pretrained_model = "./models/resnet50_places365.pth.tar"
use_cuda = True


######################################################################
# Model Under Attack
# ~~~~~~~~~~~~~~~~~~
#
# ==================== Load model ==================== #
# As mentioned, the model under attack is the ResNet model trained to recognize places.
# The purpose of this section is to define the model and dataloader,
# then initialize the model and load the pretrained weights.
#

#  Test dataset and dataloader declaration
transform_pipeline = transforms.Compose([
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406],
                             std=[0.229, 0.224, 0.225])
    ])
dataset = datasets.Places365(path_to_data=PATH_TO_DATA, path_to_labels=PATH_TO_LABELS, transform=transform_pipeline)
test_loader = torch.utils.data.DataLoader(
            dataset=dataset,
            batch_size=BATCH_SIZE,
            shuffle=True)

# Define what device we are using
print("CUDA Available: ",torch.cuda.is_available())
device = torch.device("cuda" if (use_cuda and torch.cuda.is_available()) else "cpu")

# Initialize the network and load the weights
model = models.resnet50(pretrained=False)
model.avgpool = nn.AdaptiveAvgPool2d((1, 1))
model.fc = nn.Linear(2048, 365) # To be made compatible to 365 number of classes instead of the original 1000
model.to(device)
checkpoint = load_weights(pretrained_model, device = device)
model.load_state_dict(checkpoint)

# Set the model in evaluation mode. There is no training a model when training for a defence, instead you optimize the input.
model.eval()

#--------------------------
# Aesthetics model for evaluation:
# ~~~~~~~~~~~~~~~~~~

# for child in aesthetics_model.children():
#     print(child)
base_model = models.vgg16(pretrained=False)
# old_model = NIMA(base_model)
# old_model.load_state_dict(torch.load("./NIMA/epoch-12.pkl"))

aesthetics_model = NIMA(base_model)
aesthetics_model.load_state_dict(torch.load("./NIMA/epoch-57.pkl", map_location=device))

aesthetics_model.to(device)
aesthetics_model.eval()


######################################################################
# FGSM Attack
# ~~~~~~~~~~~
#
# Now, we can define the function that creates the adversarial examples by
# perturbing the original inputs. The ``fgsm_attack`` function takes three
# inputs, *image* is the original clean image (`x`), *epsilon* is
# the pixel-wise perturbation amount (`\epsilon`), and *data_grad*
# is gradient of the loss w.r.t the input image
# (`\nabla_{x} J(\mathbf{\theta}, \mathbf{x}, y)`). The function
# then creates perturbed image as
#
# .. math:: perturbed\_image = image + epsilon*sign(data\_grad) = x + \epsilon * sign(\nabla_{x} J(\mathbf{\theta}, \mathbf{x}, y))
#
# Finally, in order to maintain the original range of the data, the
# perturbed image is clipped to range `[0,1]`.
#

# FGSM attack code
def fgsm_attack(image, epsilon, data_grad, device):
    # Collect the element-wise sign of the data gradient
    sign_data_grad = data_grad.sign()
    # Create the perturbed image by adjusting each pixel of the input image
    # print(type(image))
    if USE_MAP:
        rmap = get_reverse_saliency(image) #Multiply by the reverse saliency map to mitigate perturbation on salient parts.
        rmap_noflat = rmap
        ############
        # Using a sobel filter to find edges then blurring to spread the edginess. This gives us another map that allows us to avoid perturbing flat areas.
        from scipy import ndimage
        dx = ndimage.sobel(image.detach().cpu().squeeze(0).numpy(), 0)  # horizontal derivative
        dy = ndimage.sobel(image.detach().cpu().squeeze(0).numpy(), 1)  # vertical derivative
        mag = np.hypot(dx, dy)  # magnitude
        mag *= 255.0 / np.max(mag)  # normalize (Q&D)
        stdv = 10 # magnitude of blurring
        edginess = ndimage.filters.gaussian_filter(mag, stdv)
        edginess = np.transpose(edginess, (1,2,0))
        edginess = rgb2gray(edginess)
        edginess = torch.from_numpy(edginess).unsqueeze(0).unsqueeze(0)
        utils.save_image(minmax_normalization(edginess), os.path.join("./adv_example", "edginess.png"))
        edginess = minmax_normalization(edginess) #bring to 0-1 scale
        edginess = edginess.type(torch.FloatTensor).to(device)
        # rmap_noflat[edginess<edginess.mean()+abs(edginess.mean()/2)]=0 # Black out flat surfaces
        rmap_noflat[edginess<edginess.mean()]=0 # Black out flat surfaces
        rmap_noflat = minmax_normalization(rmap_noflat)

        perturbed_image = image + epsilon*sign_data_grad*rmap_noflat
        utils.save_image(minmax_normalization(rmap), os.path.join("./adv_example", "rmap.png"))
    else:
        rmap = None
        perturbed_image = image + epsilon*sign_data_grad

    utils.save_image(minmax_normalization(perturbed_image), os.path.join("./adv_example", "perturbed.png"))
    utils.save_image(minmax_normalization(image), os.path.join("./adv_example", "original.png"))
    # perturbed_image = torch.clamp(perturbed_image, -2, 2) # Changed to 95% confidence interval
    # Return the perturbed image
    if TILTSHIFT:
        perturbed_image = tiltshift(os.path.join("./adv_example", "perturbed.png"), os.path.join("./adv_example", "rmap.png"))
        perturbed_image = perturbed_image.convert('RGB') # important to add, By default PIL is agnostic about color spaces: https://stackoverflow.com/questions/50622180/does-pil-image-convertrgb-converts-images-to-srgb-or-adobergb/50623824
        perturbed_image = transform_pipeline(perturbed_image)
        perturbed_image = perturbed_image.unsqueeze(0)
        perturbed_image = perturbed_image.to(device)
        utils.save_image(minmax_normalization(perturbed_image), os.path.join("./adv_example", "blurred.png"))
    # to_pil = transforms.ToPILImage()
    # to_tensor = transforms.ToTensor()
    # perturbed_image = to_tensor(tiltshift(to_pil(perturbed_image.squeeze()))).unsqueeze()

    return perturbed_image, rmap
######################################################################
# Saliency & Blur
# ~~~~~~~~~~~
#

def get_reverse_saliency(img):
    _, reverse_map = infer_smap.map(img=img, weights="./salient_bluring/saliency_map_generation/salgan_salicon.pt", model=SalBCE.SalGAN(), device=device)
    return reverse_map

def tiltshift(img_path, rmap_path):
    img = Image.open(img_path)
    img = img.convert("RGB")
    rmap = Image.open(rmap_path)
    rmap = rmap.convert("RGB")
    x = miniatures.createMiniature(img, [], custom_mask=rmap)
    return x


######################################################################
# Testing Function
# ~~~~~~~~~~~~~~~~
#
# Finally, the central result of this tutorial comes from the ``test``
# function. Each call to this test function performs a full test step on
# the test set and reports a final accuracy. However, notice that
# this function also takes an *epsilon* input. This is because the
# ``test`` function reports the accuracy of a model that is under attack
# from an adversary with strength `epsilon`. More specifically, for
# each sample in the test set, the function computes the gradient of the
# loss w.r.t the input data (`data_grad`), creates a perturbed
# image with ``fgsm_attack`` (`perturbed_data`), then checks to see
# if the perturbed example is adversarial. In addition to testing the
# accuracy of the model, the function also saves and returns some
# successful adversarial examples to be visualized later.
#


def test( model, device, test_loader, epsilon ):

    start = datetime.datetime.now().replace(microsecond=0)

    # Accuracy counter
    correct = 0
    wrong_counter = 0
    adv_examples, original_aesthetics, final_aesthetics = [], [], []
    num_samples = 100#len(test_loader)
    print("Initiating test with epsilon {} and tiltshift set to {}".format(epsilon, TILTSHIFT))
    # Loop over all examples in test set
    for i, (data, target, ID) in enumerate(test_loader):
        if i == num_samples:
            break
        # Send the data and label to the device
        data, target = data.to(device), target.to(device)
        # Set requires_grad attribute of tensor. Important for Attack
        data.requires_grad = True

        # Forward pass the data through the model
        output = model(data)
        init_pred = F.softmax(output, dim=1)
        init_pred = init_pred.max(1, keepdim=True)[1] # get the index of the max log-probability

        if init_pred.item() != target.item():
            continue
        # print(F.log_softmax(output, dim=1).exp()) #Gives one hot encoding


        # Calculate the loss
        loss = F.nll_loss(output, target)
        # Zero all existing gradients
        model.zero_grad()

        # Calculate gradients of model in backward pass
        loss.backward()

        # Collect datagrad
        data_grad = data.grad.data

        # Call FGSM Attack
        perturbed_data, rmap = fgsm_attack(data, epsilon, data_grad, device)

        # Re-classify the perturbed image
        output = model(perturbed_data)

        # Check for success
        final_pred = F.softmax(output, dim=1).max(1, keepdim=True)[1] # get the index of the max log-probability
        if final_pred.item() == target.item():
            correct += 1
            # Special case for saving 0 epsilon examples
            if (epsilon == 0) and (len(adv_examples) < 5):
                original_ex = data.squeeze().detach().cpu()#.numpy()
                adv_ex = perturbed_data.squeeze().detach().cpu()#.numpy()
                if USE_MAP:
                    rmap = rmap.squeeze().detach().cpu()#.numpy()
                adv_examples.append( (original_ex, adv_ex, rmap) )
        else:
            # Save some adv examples for visualization later
            # print("got some")
            if len(adv_examples) < 5:

                original_ex = data.squeeze().detach().cpu()#.numpy()
                adv_ex = perturbed_data.squeeze().detach().cpu()#.numpy()
                if USE_MAP:
                    rmap = rmap.squeeze().detach().cpu()#.numpy()
                adv_examples.append( (original_ex, adv_ex, rmap) )


        data_copy, perturbed_copy = data.detach(), perturbed_data.detach() # Have to detach from graph that was developed for the attack model or get memory errors.
        before = aesthetics_model(data_copy).detach().cpu()
        after = aesthetics_model(perturbed_copy).detach().cpu()
        predicted_mean_before = 0
        # print(before[0])
        for i, elem in enumerate(before[0]):
            predicted_mean_before += i * elem
        predicted_mean_after = 0
        for i, elem in enumerate(after[0]):
            predicted_mean_after += i * elem
        original_aesthetics.append(predicted_mean_before)
        final_aesthetics.append(predicted_mean_after)
        # print(predicted_mean_before)

    # Calculate final accuracy for this epsilon
    final_acc = correct/float(num_samples)
    original_aes_score = np.mean(original_aesthetics)
    final_aes_score = np.mean(final_aesthetics)

    # print("Wrong percentage: {}".format(wrong_counter/num_samples))
    # Return the accuracy and an adversarial example
    end = datetime.datetime.now().replace(microsecond=0)

    print("Epsilon: {}\tTest Accuracy = {} / {} = {}\t Time elapsed: {}".format(epsilon, correct, num_samples, final_acc, end-start))
    print("Original/Final Avg of Aesthetics: {}/{}".format(original_aes_score, final_aes_score))
    logfile = open("logfile.txt","a")
    logfile.write("Epsilon: {}\tTest Accuracy = {} / {} = {}\t Time elapsed: {} \n".format(epsilon, correct, num_samples, final_acc, end-start))
    logfile.write("Original/Final Avg of Aesthetics: {}/{} \n".format(original_aes_score, final_aes_score))
    logfile.close()

    return final_acc, adv_examples


######################################################################
# Run Attack
# ~~~~~~~~~~
#
# The last part of the implementation is to actually run the attack. Here,
# we run a full test step for each epsilon value in the *epsilons* input.
# For each epsilon we also save the final accuracy and some successful
# adversarial examples to be plotted in the coming sections. Notice how
# the printed accuracies decrease as the epsilon value increases. Also,
# note the `\epsilon=0` case represents the original test accuracy,
# with no attack.
#

accuracies = []
examples = []

# Run test for each epsilon
for eps in epsilons:
    acc, ex = test(model, device, test_loader, eps)
    accuracies.append(acc)
    examples.append(ex)


######################################################################
# Results
# -------
#
# Accuracy vs Epsilon
# ~~~~~~~~~~~~~~~~~~~
#
# The first result is the accuracy versus epsilon plot. As alluded to
# earlier, as epsilon increases we expect the test accuracy to decrease.
# This is because larger epsilons mean we take a larger step in the
# direction that will maximize the loss. Notice the trend in the curve is
# not linear even though the epsilon values are linearly spaced. For
# example, the accuracy at `\epsilon=0.05` is only about 4% lower
# than `\epsilon=0`, but the accuracy at `\epsilon=0.2` is 25%
# lower than `\epsilon=0.15`. Also, notice the accuracy of the model
# hits random accuracy for a 10-class classifier between
# `\epsilon=0.25` and `\epsilon=0.3`.
#

plt.figure(figsize=(5,5))
plt.plot(epsilons, accuracies, "*-")
plt.yticks(np.arange(0, 1.1, step=0.1))
plt.xticks(np.arange(epsilons[0], epsilons[-1], step=epsilons[1]-epsilons[0]))
plt.title("Tiltshift")
plt.xlabel("Epsilon")
plt.ylabel("Accuracy")
plt.savefig("Epsilons.png")
# plt.show()


######################################################################
# Sample Adversarial Examples
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~
#
# Remember the idea of no free lunch? In this case, as epsilon increases
# the test accuracy decreases **BUT** the perturbations become more easily
# perceptible. In reality, there is a tradeoff between accuracy
# degredation and perceptibility that an attacker must consider. Here, we
# show some examples of successful adversarial examples at each epsilon
# value. Each row of the plot shows a different epsilon value. The first
# row is the `\epsilon=0` examples which represent the original
# “clean” images with no perturbation. The title of each image shows the
# “original classification -> adversarial classification.” Notice, the
# perturbations start to become evident at `\epsilon=0.15` and are
# quite evident at `\epsilon=0.3`. However, in all cases humans are
# still capable of identifying the correct class despite the added noise.
#

# # Plot several examples of adversarial samples at each epsilon
# cnt = 0
# plt.figure(figsize=(8,10))
for i in range(len(epsilons)):
    for j in range(len(examples[i])):
        orig, ex, rmap = examples[i][j]

        utils.save_image(minmax_normalization(orig), "./adv_example/original_e{}.png".format(epsilons[i]))
        utils.save_image(minmax_normalization(ex), "./adv_example/example_e{}.png".format(epsilons[i]))

        if USE_MAP:
            utils.save_image(minmax_normalization(rmap), "./adv_example/rsmap_e{}.png".format(epsilons[i]))

#         cnt += 1
#         plt.subplot(len(epsilons),len(examples[0]),cnt)
#         plt.xticks([], [])
#         plt.yticks([], [])
#         if j == 0:
#             plt.ylabel("Eps: {}".format(epsilons[i]), fontsize=14)
#         plt.title("{} -> {}".format(orig, adv))
#         plt.imshow(ex, cmap="gray")
# plt.tight_layout()
# plt.savefig("QAnal.png")
# plt.show()

