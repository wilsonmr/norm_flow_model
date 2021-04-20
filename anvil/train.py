"""
train.py

Module containing functions required to train model
"""

from tqdm import tqdm

import signal
import sys

import torch
import torch.optim as optim

from anvil.utils import get_num_parameters

import logging

log = logging.getLogger(__name__)


def handler(signum, frame):
    """Handles keyboard interruptions and terminations and exits in such a way that,
    if the program is currently inside a try-except-finally block, the finally clause
    will be executed."""
    sys.exit(1)


signal.signal(signal.SIGTERM, handler)  # termination
signal.signal(signal.SIGINT, handler)  # keyboard interrupt


def save_checkpoint(outpath, epoch, loss, model, optimizer, scheduler):
    torch.save(
        {
            "epoch": epoch,
            "loss": loss,
            "model_state_dict": model.state_dict(),
            "optimizer_state_dict": optimizer.state_dict(),
            "scheduler_state_dict": scheduler.state_dict(),
        },
        f"{outpath}/checkpoint_{epoch}.pt",
    )
    tqdm.write(f"Checkpoint saved at epoch {epoch}")


def reverse_kl(
    model_log_density: torch.Tensor, target_log_density: torch.Tensor
) -> torch.Tensor:
    r"""Estimate of the reverse Kullbach-Leibler divergence between the model
    and the target density, obtained by averaging over a batch of configurations
    generated by the model.

    Parameters
    ----------
    model_log_density: torch.Tensor
        column of the logarithm of the probability density for the batch of
        configurations generated by the model, as given by the change of
        variables formula.
    target_log_density: torch.Tensor
        column containing the logarithm of the probability density for the
        batch of configurations, defined by the action of the field theory.

    Returns
    -------
    out: torch.Tensor
        torch tensor with single element, corresponding to the estimate of the
        reverse KL divergence.

    Notes
    -----
    Only terms which depend on the parameters of the model are necessary for
    optimisation. Hence, model_log_density could be simply the logarithm of the
    Jacobian determinant of the learnable layers in the normalising flow, and
    target_log_density need only be the negative action.
    """
    return torch.mean(model_log_density - target_log_density, dim=0)


def training_update(
    loaded_model,
    base,
    target,
    n_batch,
    current_loss,
    loaded_optimizer,
    loaded_scheduler,
):
    """A single training update or 'epoch'."""
    # generate latent variables
    z = base(n_batch)

    # pick out configs whose d.o.f sum to < 0
    # TODO: annoying to do this in the training loop
    # TODO: replace with call to function which returns *extra_args for loaded_model,
    # which is chosen by user based on theory being studied.
    negative_mag = (z.sum(dim=1).sign() < 0).nonzero().squeeze()

    # Transform latents -> candidate configurations (gradients tracked)
    phi, log_det_jacob = loaded_model(z, 0, negative_mag)

    # compute loss function (gradients tracked)
    target_log_density = target.log_density(phi)
    current_loss = reverse_kl(log_det_jacob, target_log_density)

    # backprop and step model parameters
    loaded_optimizer.zero_grad()  # zero gradients from prev minibatch
    current_loss.backward()  # accumulate new gradients
    loaded_optimizer.step()

    # TODO: we have different scheduler updates depending on which we're using.
    # loaded_scheduler.step(current_loss)
    loaded_scheduler.step()

    return current_loss


def train(
    loaded_model,
    base,
    target,
    *,
    train_range,
    n_batch,
    outpath,
    current_loss,
    loaded_optimizer,
    loaded_scheduler,
    save_interval=1000,
):
    """Loop over training updates, periodically saving checkpoints."""

    # Let the user know the total number of model parameters
    # TODO: should have a --verbose option which controls whether we care about stuff like this
    num_parameters = get_num_parameters(loaded_model)
    log.info(f"Model has {num_parameters} trainable parameters.")

    current_epoch, final_epoch = train_range
    pbar = tqdm(total=(final_epoch - current_epoch))

    # try, finally statement allows us to save checkpoint in case of unexpected SIGTERM or SIGINT
    try:
        while current_epoch < final_epoch:

            if (current_epoch % save_interval) == 0:
                save_checkpoint(
                    outpath,
                    epoch=current_epoch,
                    loss=current_loss,
                    model=loaded_model,
                    optimizer=loaded_optimizer,
                    scheduler=loaded_scheduler,
                )

            current_loss = training_update(
                loaded_model,
                base,
                target,
                n_batch,
                current_loss,
                loaded_optimizer,
                loaded_scheduler,
            )
            # Increment counter immediately after training update
            current_epoch += 1
            pbar.update()

            if (current_epoch % 25) == 0:
                pbar.set_description(f"loss: {current_loss.item()}")

                # TODO again want some flag that controls whether to save this data
                with open("loss.txt", "a") as f:
                    f.write(f"{float(current_loss)}\n")

    finally:  # executed after while loop, or if sys.exit is called
        save_checkpoint(
            outpath,
            epoch=current_epoch,
            loss=current_loss,
            model=loaded_model,
            optimizer=loaded_optimizer,
            scheduler=loaded_scheduler,
        )

    return loaded_model
