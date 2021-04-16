r"""
layers.py

Contains nn.Modules which implement transformations of input configurations whilst computing
the Jacobian determinant of the transformation.

Each transformation layers may contain several neural networks or learnable parameters.

A normalising flow, f, can be constructed from multiple layers using function composition:

        f(z) = g_n( ... ( g_2( g_1( z ) ) ) ... )

which is implemented using the architecture provided by torch.nn

All layers in this module contain a `forward` method which takes two torch.tensor objects
as inputs:

    - a batch of input configurations, dimensions (batch size, lattice size).

    - a batch of scalars, dimensions (batch size, 1), that are the logarithm of the
      'current' probability density, at this stage in the normalising flow.

and returns two torch.tensor objects:

    - a batch of configurations \phi which have been transformed according to the 
      transformation, with the same dimensions as the input configurations.

    - the updated logarithm of the probability density, including the contribution from
      the Jacobian determinant of this transformation.
"""
import torch
import torch.nn as nn
from torchsearchsorted import searchsorted
from math import pi

from anvil.core import FullyConnectedNeuralNetwork

import numpy as np


class CouplingLayer(nn.Module):
    """
    Base class for coupling layers.

    A generic coupling layer takes the form

        v^P <- v^P                  passive partition
        v^A <- C( v^A ; {N(v^P)} )  active partition

    where the |\Lambda|-dimensional input configuration or 'vector' v has been split
    into two partitions, labelled by A and P (active and passive). Here, the paritions
    are split according to a checkerboard (even/odd) scheme.

    {N(v^P)} is a set of functions of the passive partition (neural networks) that
    parameterise the coupling layer.

    Parameters
    ----------
    size_half: int
        Half of the configuration size, which is the size of the input vector
        for the neural networks.
    even_sites: bool
        dictates which half of the data is transformed as a and b, since
        successive affine transformations alternate which half of the data is
        passed through neural networks.

    ################Parameters
    ----------
    size_half: int
        Half of the configuration size, which is the size of the input vector for the
        neural networks.
    hidden_shape: list
        list containing hidden vector sizes for the neural networks.
    activation: str
        string which is a key for an activation function for all but the final layers
        of the networks.
    s_final_activation: str
        string which is a key for an activation function, which the output of the s
        network will be passed through.
    even_sites: bool
        dictates which half of the data is transformed as a and b, since successive
        affine transformations alternate which half of the data is passed through
        neural networks.


    Attributes
    ----------
    a_ind: slice (protected)
        Slice object which can be used to access the passive partition.
    b_ind: slice (protected)
        Slice object which can be used to access the partition that gets transformed.
    join_func: function (protected)
        Function which returns the concatenation of the two partitions in the
        appropriate order.
    """

    def __init__(self, size_half: int, even_sites: bool):
        super().__init__()

        if even_sites:
            # a is first half of input vector
            self._passive_ind = slice(0, size_half)
            self._active_ind = slice(size_half, 2 * size_half)
            self._join_func = torch.cat
        else:
            # a is second half of input vector
            self._passive_ind = slice(size_half, 2 * size_half)
            self._active_ind = slice(0, size_half)
            self._join_func = lambda a, *args, **kwargs: torch.cat(
                (a[1], a[0]), *args, **kwargs
            )


class AdditiveLayer(CouplingLayer):
    r"""Extension to `nn.Module` for an additive coupling layer.

    The additive transformation is given by

        C( v^A ; t(v^P) ) = v^A - t(v^P)

    The Jacobian determinant is

        \log \det J = 0
    """

    def __init__(
        self,
        size_half: int,
        *,
        hidden_shape: list,
        activation: str,
        z2_equivar: bool,
        even_sites: bool,
    ):
        super().__init__(size_half, even_sites)

        self.t_network = FullyConnectedNeuralNetwork(
            size_in=size_half,
            size_out=size_half,
            hidden_shape=hidden_shape,
            activation=activation,
            no_final_activation=True,
            bias=not z2_equivar,
        )

    def forward(self, v_in, log_density, *unused) -> torch.Tensor:
        r"""Forward pass of affine transformation."""
        v_in_passive = v_in[:, self._passive_ind]
        v_in_active = v_in[:, self._active_ind]

        t_out = self.t_network(
            (v_in_passive - v_in_passive.mean()) / v_in_passive.std()
        )

        v_out = self._join_func([v_in_passive, v_in_active - t_out], dim=1)

        return v_out, log_density


class AffineLayer(CouplingLayer):
    r"""Extension to `nn.Module` for an affine coupling layer.

    The affine transformation is given by

        C( v^A ; s(v^P), t(v^P) ) = ( v^A - t(v^P) ) * \exp( -s(v^P) )

    The Jacobian determinant is

        \log \det J = \sum_x s_x(v^P)

    where x are the lattice sites in the active partition.

    """

    def __init__(
        self,
        size_half: int,
        *,
        hidden_shape: list,
        activation: str,
        z2_equivar: bool,
        even_sites: bool,
    ):
        super().__init__(size_half, even_sites)

        self.s_network = FullyConnectedNeuralNetwork(
            size_in=size_half,
            size_out=size_half,
            hidden_shape=hidden_shape,
            activation=activation,
            no_final_activation=False,
            bias=not z2_equivar,
        )
        self.t_network = FullyConnectedNeuralNetwork(
            size_in=size_half,
            size_out=size_half,
            hidden_shape=hidden_shape,
            activation=activation,
            no_final_activation=True,
            bias=not z2_equivar,
        )
        # NOTE: Could potentially have non-default inputs for s and t networks
        # by adding dictionary of overrides - e.g. s_options = {}

        self.z2_equivar = z2_equivar

    def forward(self, v_in, log_density, *unused) -> torch.Tensor:
        r"""Forward pass of affine transformation."""
        v_in_passive = v_in[:, self._passive_ind]
        v_in_active = v_in[:, self._active_ind]
        v_for_net = (v_in_passive - v_in_passive.mean()) / v_in_passive.std()

        s_out = self.s_network(v_for_net)
        t_out = self.t_network(v_for_net)

        # If enforcing s(-v) = -s(v), we want to use |s(v)| in affine transf.
        if self.z2_equivar:
            s_out.abs_()

        v_out = self._join_func(
            [v_in_passive, (v_in_active - t_out) * torch.exp(-s_out)], dim=1
        )
        log_density += s_out.sum(dim=1, keepdim=True)

        return v_out, log_density


class LinearSplineLayer(CouplingLayer):
    r"""A coupling transformation from [0, 1] -> [0, 1] based on a piecewise linear function.

    The linear spline transformation is

        C( v^A ; {P_k(v^P) | k = 1, ..., K} ) = \sum_k \alpha_k P_k

    where

        \alpha_k = 1                        for k < k*
        \alpha_k = ( v^A - (k-1)*W ) / W    for k = k*
        \alpha_k = 0                        for k > k*

    W = 1/K is the width of the spline segments, and k* is the segment in which y^A resides.

    The Jacobian determinant is

        \log \det J = -\sum_x \log P_{k*, x} + const.

    Notes
    -----
    P_k can be interpreted as probability masses for K equally-sized 'bins' on the interval
    [0, 1]. Then, the transformation is the cumulative distribution function of this
    probability distribution.
    """

    def __init__(
        self,
        size_half: int,
        *,
        n_segments: int,
        hidden_shape: list,
        activation: str,
        even_sites: bool,
    ):
        super().__init__(size_half, even_sites)
        self.size_half = size_half

        self.n_segments = n_segments
        self.width = 1 / n_segments

        # x coordinates of the knots are just n/K for n = 0, ..., K
        self.knots_xcoords = torch.linspace(0, 1, n_segments + 1).view(1, -1)

        self.network = FullyConnectedNeuralNetwork(
            size_in=size_half,
            size_out=size_half * n_segments,
            hidden_shape=hidden_shape,
            activation=activation,
            no_final_activation=False,
            bias=True,
        )
        self.norm_func = nn.Softmax(dim=2)

    def forward(self, v_in, log_density, *unused):
        """Forward pass of the linear spline layer."""
        v_in_passive = v_in[:, self._passive_ind]
        v_in_active = v_in[:, self._active_ind]

        net_out = self.norm_func(
            self.network(v_in_passive - 0.5).view(-1, self.size_half, self.n_segments)
        )
        # Build the y coordinates of the knots
        knots_ycoords = torch.cat(
            (
                torch.zeros(-1, self.size_half, 1),
                torch.cumsum(net_out, dim=2),
            ),
            dim=2,
        )

        # Sort inputs v_in_active into the appropriate segment or 'bin'
        # NOTE: need to make v_in_active contiguous, otherwise searchsorted returns nonsense
        k_this_segment = (
            searchsorted(self.knots_xcoords, v_in_active.contiguous().view(-1, 1)) - 1
        ).clamp(0, self.n_segments - 1)

        # Get P_k*, alpha_k and the value v would take at the lower knot point
        p_this_segment = torch.gather(net_out, 1, k_this_segment)
        v_out_at_lower_knot = torch.gather(knots_ycoords, 1, k_this_segment)
        alpha = (
            v_in_active.unsqueeze(dim=-1) - k_this_segment * self.width
        ) / self.width

        v_out = self._join_func(
            [
                v_in_passive,
                (v_out_at_lower_knot + alpha * p_this_segment).squeeze(),
            ],
            dim=1,
        )
        log_density -= torch.log(p_this_segment).sum(dim=1)

        return v_out, log_density


class QuadraticSplineLayer(CouplingLayer):
    r"""A coupling transformation from [0, 1] -> [0, 1] based on a piecewise quadratic function.

    The quadratic spline transformation is

        C( v^A ; {h_0(v^P), h_k(v^P), w_k(v^P) | k = 1, ..., K} ) =

                \sum_{k=1}^{k*-1} 1/2 ( h_{k-1} + h_k ) w_k
              + \alpha h_{k*-1} w_k*
              + 1/2 \alpha^2 (h_k* - h_{k*-1}) w_k

    where \alpha = ( v^A - \sum_{k=1}^{k*-1} 1/2 ( h_{k-1} + h_k ) w_k ) / w_k*

    Notes
    -----
    The interval is divided into K segments (bins), with K+1 knot points (bin boundaries).
    The coupling transformation is defined piecewise by the unique polynomials whose
    end-points are the knot points.

    A neural network generates K+1 values for the y-positions (heights) at the x knot points,
    and K bin widths. The transformation is then defined as the cumulative distribution
    function associated with the piecewise linear probability density function obtained by
    interpolating between the heights.
    """

    def __init__(
        self,
        size_half: int,
        n_segments: int,
        hidden_shape: list,
        activation: str,
        even_sites: bool,
    ):
        super().__init__(size_half, even_sites)
        self.size_half = size_half
        self.n_segments = n_segments

        self.network = FullyConnectedNeuralNetwork(
            size_in=size_half,
            size_out=size_half * (2 * n_segments + 1),
            hidden_shape=hidden_shape,
            activation=activation,
            no_final_activation=False,
            bias=True,
        )
        self.w_norm_func = nn.Softmax(dim=2)

    @staticmethod
    def h_norm_func(h_net, w_norm):
        """Normalisation function for height values.

        h_k <- \exp(h_k) / (
                \sum_{k'=1}^K 1/2 w_k' ( \exp(h_{k'-1}) + \exp(h_k') )
            )
        """
        return torch.exp(h_net) / (
            0.5 * w_norm * (torch.exp(h_out[..., :-1]) + torch.exp(h_out[..., 1:]))
        ).sum(dim=2, keepdim=True)

    def forward(self, v_in, log_density, *unused):
        """Forward pass of the quadratic spline layer."""
        v_in_passive = v_in[:, self._passive_ind]
        v_in_active = v_in[:, self._active_ind]

        h_net, w_net = (
            self.network(v_in_passive - 0.5)
            .view(-1, self.size_half, 2 * self.n_segments + 1)
            .split((self.n_segments + 1, self.n_segments), dim=2)
        )
        w_norm = self.w_norm_func(w_net)
        h_norm = self.h_norm_func(h_net, w_norm)

        # x and y coordinates of the spline knots
        knots_xcoords = torch.cat(
            (
                torch.zeros(h_norm.shape[0], self.size_half, 1),
                torch.cumsum(w_norm, dim=2),
            ),
            dim=2,
        )
        knots_ycoords = torch.cat(
            (
                torch.zeros(h_norm.shape[0], self.size_half, 1),
                torch.cumsum(
                    0.5 * w_norm * (h_norm[..., :-1] + h_norm[..., 1:]),
                    dim=2,
                ),
            ),
            dim=2,
        )

        # Temporarily mix batch and lattice dimensions so that the bisection search
        # can be done in a single operation
        k_this_segment = (
            searchsorted(
                knots_xcoords.contiguous(),
                v_in_b_inside_interval.contiguous().view(-1, 1),
            )
            - 1
        ).clamp(0, self.n_segments - 1)

        w_this_segment = torch.gather(w_norm, 1, k_this_segment)
        h_at_lower_knot = torch.gather(h_norm, 1, k_this_segment)
        h_at_upper_knot = torch.gather(h_norm, 1, k_this_segment + 1)

        v_in_at_lower_knot = torch.gather(knots_xcoords, 1, k_this_segment)
        v_out_at_lower_knot = torch.gather(knots_ycoords, 1, k_this_segment)

        alpha = (v_in_active.unsqueeze(dim=-1) - v_in_at_lower_knot) / w_this_segment

        v_out = self._join_func(
            [
                v_in_passive,
                (
                    v_out_at_lower_knot
                    + alpha * h_at_lower_knot * w_this_segment
                    + 0.5
                    * alpha.pow(2)
                    * (h_at_upper_knot - h_at_lower_knot)
                    * w_this_segment
                ).squeeze(),
            ],
            dim=1,
        )
        log_density -= torch.log(
            h_at_lower_knot + alpha * (h_at_upper_knot - h_at_lower_knot)
        ).sum(dim=1)

        return v_out, log_density


class RationalQuadraticSplineLayer(CouplingLayer):
    r"""A coupling transformation from a finite interval to itself based on a piecewise
    rational quadratic spline function.

    The interval is divided into K segments (bins) with widths w_k and heights h_k. The
    'knot points' (\phi_k, x_k) are the cumulative sum of (h_k, w_k), starting at (-B, -B)
    and ending at (B, B).

    In addition to the w_k and h_k, the derivatives d_k at the internal knot points are
    generated by a neural network. d_0 and d_K are set to 1.

    Defing the slopes s_k = h_k / w_k and fractional position within a bin

            alpha(x) = (x - x_{k-1}) / w_k

    the coupling transformation is defined piecewise by

            C(v^A, {h_k, s_k, d_k | k = 1, ..., K})
                 = \phi_{k-1}
                 + ( h_k(s_k * \alpha^2 + d_k * \alpha * (1 - \alpha)) )
                 / ( s_k + (d_{k+1} + d_k - 2s_k) * \alpha * (1 - \alpha) )
    """
    # TODO sort out indices

    def __init__(
        self,
        size_half: int,
        interval: int,
        n_segments: int,
        hidden_shape: list,
        activation: str,
        z2_equivar: bool,
        even_sites: bool,
    ):
        super().__init__(size_half, even_sites)
        self.size_half = size_half
        self.n_segments = n_segments

        self.network = FullyConnectedNeuralNetwork(
            size_in=size_half,
            size_out=size_half * (3 * n_segments - 1),
            hidden_shape=hidden_shape,
            activation=activation,
            no_final_activation=False,
            bias=True,
        )

        self.norm_func = nn.Softmax(dim=1)
        self.softplus = nn.Softplus()

        self.B = interval

        self.z2_equivar = z2_equivar

    def forward(self, v_in, log_density, negative_mag):
        """Forward pass of the rational quadratic spline layer."""
        v_in_passive = v_in[:, self._passive_ind]
        v_in_active = v_in[:, self._active_ind]
        v_for_net = (
            v_in_passive - v_in_passive.mean()
        ) / v_in_passive.std()  # reduce numerical instability

        # Naively enforce C(-v) = -C(v)
        if self.z2_equivar:
            v_in_passive_stand[negative_mag] = -v_in_passive_stand[negative_mag]

        v_out_b = torch.zeros_like(v_in_active)
        gradient = torch.ones_like(v_in_active).unsqueeze(dim=-1)

        # Apply mask for linear tails
        # NOTE potentially a waste of time since we NEVER want to map out <- Id(in)
        inside_interval_mask = torch.abs(v_in_active) <= self.B
        v_in_b_inside_interval = v_in_active[inside_interval_mask]
        v_out_b[~inside_interval_mask] = v_in_active[~inside_interval_mask]

        h_net, w_net, d_net = (
            self.network(v_for_net)
            .view(-1, self.size_half, 3 * self.n_segments - 1)
            .split(
                (self.n_segments, self.n_segments, self.n_segments - 1),
                dim=2,
            )
        )

        if self.z2_equivar:
            h_net[negative_mag] = torch.flip(h_net[negative_mag], dims=(2,))
            w_net[negative_mag] = torch.flip(w_net[negative_mag], dims=(2,))
            d_net[negative_mag] = torch.flip(d_net[negative_mag], dims=(2,))

        h_norm = self.norm_func(h_net[inside_interval_mask]) * 2 * self.B
        w_norm = self.norm_func(w_net[inside_interval_mask]) * 2 * self.B
        d_pad = nn.functional.pad(
            self.softplus(d_raw)[inside_interval_mask], (1, 1), "constant", 1
        )

        knots_xcoords = (
            torch.cat(
                (
                    torch.zeros(w_norm.shape[0], 1),
                    torch.cumsum(w_norm, dim=1),
                ),
                dim=1,
            )
            - self.B
        )
        knots_ycoords = (
            torch.cat(
                (
                    torch.zeros(h_norm.shape[0], 1),
                    torch.cumsum(h_norm, dim=1),
                ),
                dim=1,
            )
            - self.B
        )

        k_this_segment = (
            searchsorted(
                knots_xcoords.contiguous(),
                v_in_b_inside_interval.contiguous().view(-1, 1),
            )
            - 1
        ).clamp(0, self.n_segments - 1)

        w_at_lower_knot = torch.gather(w_norm, 1, k_this_segment)
        h_at_lower_knot = torch.gather(h_norm, 1, k_this_segment)
        s_at_lower_knot = h_at_lower_knot / w_at_lower_knot
        d_at_lower_knot = torch.gather(d_pad, 1, k_this_segment)
        d_at_upper_knot = torch.gather(d_pad, 1, k_this_segment + 1)

        v_in_at_lower_knot = torch.gather(knots_xcoords, 1, k_this_segment)
        v_out_at_lower_knot = torch.gather(knots_ycoords, 1, k_this_segment)

        alpha = (
            v_in_b_inside_interval.unsqueeze(dim=-1) - v_in_at_lower_knot
        ) / w_at_lower_knot

        v_out_b[inside_mask] = (
            v_out_at_lower_knot
            + (
                h_at_lower_knot
                * (
                    s_at_lower_knot * alpha.pow(2)
                    + d_at_lower_knot * alpha * (1 - alpha)
                )
            )
            / (
                s_at_lower_knot
                + (d_at_upper_knot + d_at_lower_knot - 2 * s_at_lower_knot)
                * alpha
                * (1 - alpha)
            )
        ).squeeze()

        gradient[inside_mask] = (
            s_at_lower_knot.pow(2)
            * (
                d_at_upper_knot * alpha.pow(2)
                + 2 * s_at_lower_knot * alpha * (1 - alpha)
                + d_at_lower_knot * (1 - alpha).pow(2)
            )
        ) / (
            s_at_lower_knot
            + (d_at_upper_knot + d_at_lower_knot - 2 * s_at_lower_knot)
            * alpha
            * (1 - alpha)
        ).pow(
            2
        )

        v_out = self._join_func([v_in_passive, v_out_b], dim=1)
        log_density -= torch.log(gradient).sum(dim=1)

        return v_out, log_density


class GlobalAffineLayer(nn.Module):
    r"""Applies an affine transformation to every data point using a given scale and shift,
    which are *not* learnable. Useful to shift the domain of a learned distribution. This is
    done at the cost of a constant term in the logarithm of the Jacobian determinant, which
    is ignored.

    Parameters
    ----------
    scale: (int, float)
        Every data point will be multiplied by this factor.
    shift: (int, float)
        Every scaled data point will be shifted by this factor.
    """

    def __init__(self, scale, shift):
        super().__init__()
        self.scale = scale
        self.shift = shift

    def forward(self, v_in, log_density):
        """Forward pass of the global affine transformation."""
        return self.scale * v_in + self.shift, log_density


# TODO not necessary to define a nn.module for this now I've taken otu learnable gamma
class BatchNormLayer(nn.Module):
    """Performs batch normalisation on the input vector.

    Parameters
    ----------
    scale: int
        An additional scale factor to be applied after batch normalisation.
    """

    def __init__(self, scale=1):
        super().__init__()
        self.gamma = scale

    def forward(self, v_in, log_density, *unused):
        """Forward pass of the batch normalisation transformation."""

        v_out = self.gamma * (v_in - v_in.mean()) / torch.std(x_in)

        return (
            phi_out,
            log_density,
        )  # don't need to update log dens - nothing to optimise
