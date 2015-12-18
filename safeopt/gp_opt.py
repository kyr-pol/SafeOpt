"""
Classes that implement SafeOpt.

Author: Felix Berkenkamp (befelix at inf dot ethz dot ch)
"""

from __future__ import print_function, absolute_import, division

from .utilities import *

import sys
import numpy as np                          # ...
import scipy as sp
import GPy                                  # GPs
from GPy.util.linalg import dpotrs          # For rank-1 updates
from GPy.inference.latent_function_inference.posterior import Posterior
import matplotlib.pyplot as plt             # Plotting
from collections import Sequence            # isinstance(...,Sequence)
from matplotlib import cm                   # 3D plot colors
from scipy.spatial.distance import cdist    # Efficient distance computation
from mpl_toolkits.mplot3d import Axes3D     # Create 3D axes


__all__ = ['SafeOpt', 'GaussianProcessOptimization']


# For python 2 (python 3 is not yet supported by GPy)
if sys.version_info[0] < 3:
    range = xrange


class GaussianProcessOptimization(object):
    """
    Base class for GP optimization.

    Handles common functionality.

    Parameters:
    -----------
    function: object
        A function that returns the current value that we want to optimize.
    gp: GPy Gaussian process
    parameter_set: 2d-array
        List of parameters
    beta: float or callable
        A constant or a function of the time step that scales the confidence
        interval of the acquisition function.
    """
    def __init__(self, function, gp, parameter_set, beta):
        super(GaussianProcessOptimization, self).__init__()

        self.gp = gp
        self.kernel = gp.kern
        self.likelihood = gp.likelihood
        self.function = function

        if hasattr(beta, '__call__'):
            # Beta is a function of t
            self.beta = beta
        else:
            # Assume that beta is a constant
            self.beta = lambda t: beta

        self._inputs = None
        self.bounds = None
        self.num_samples = 0
        self.inputs = parameter_set.copy()

        # Time step
        self.t = 0

    @property
    def inputs(self):
        """Discrete parameter samples for Bayesian optimization."""
        return self._inputs

    @inputs.setter
    def inputs(self, parameter_set):
        self._inputs = parameter_set

        # Plotting bounds (min, max value
        self.bounds = list(zip(np.min(self._inputs, axis=0),
                               np.max(self._inputs, axis=0)))
        self.num_samples = [len(np.unique(self._inputs[:, i]))
                            for i in range(self._inputs.shape[1])]

    def plot(self, axis=None, figure=None, n_samples=None, plot_3d=False,
             **kwargs):
        """
        Plot the current state of the optimization.

        Parameters
        ----------
        axis: matplotlib axis
            The axis on which to draw (does not get cleared first)
        figure: matplotlib figure
            Ignored if axis is already defined
        n_samples: int
            How many samples to use for plotting (uses input parameters if
            None)
        plot_3d: boolean
            If set to true shows a 3D plot for 2 dimensional data
        """
        # 4d plots are tough...
        if self.kernel.input_dim > 2:
            return None

        if n_samples is None:
            inputs = self.inputs
            n_samples = self.num_samples
        else:
            inputs = linearly_spaced_combinations(self.bounds,
                                                  n_samples)
            if not isinstance(n_samples, Sequence):
                n_samples = [n_samples] * len(self.bounds)

        if axis is None:
            if figure is None:
                fig = plt.figure()
            else:
                fig = figure

            if plot_3d:
                axis = Axes3D(fig)
            else:
                axis = fig.gca()

        if self.kernel.input_dim > 1:   # 3D plot
            if plot_3d:
                output, var = self.gp._raw_predict(inputs)
                # output += 2 * np.sqrt(var)

                axis.plot_trisurf(inputs[:, 0], inputs[:, 1], output[:, 0],
                                  cmap=cm.jet, linewidth=0.2, alpha=0.5)

                axis.plot(self.gp.X[:, 0], self.gp.X[:, 1], self.gp.Y[:, 0],
                          'o')

            else:
                # Use 2D level set plot, 3D is too slow
                output, var = self.gp._raw_predict(inputs)
                if np.all(output == output[0, 0]):
                    plt.xlim(self.bounds[0])
                    plt.ylim(self.bounds[1])
                    return None
                c = axis.contour(np.linspace(self.bounds[0][0],
                                           self.bounds[0][1],
                                           n_samples[0]),
                               np.linspace(self.bounds[1][0],
                                           self.bounds[1][1],
                                           n_samples[1]),
                               output.reshape(*n_samples),
                               20)
                plt.colorbar(c)
                axis.plot(self.gp.X[:, 0], self.gp.X[:, 1], 'ob')

        else:   # 2D plots with uncertainty
            output, var = self.gp._raw_predict(inputs)
            output = output.squeeze()
            std_dev = self.beta(self.t) * np.sqrt(var.squeeze())
            axis.fill_between(inputs[:, 0],
                              output - std_dev,
                              output + std_dev,
                              facecolor='blue',
                              alpha=0.3)
            axis.plot(inputs[:, 0], output, **kwargs)
            axis.plot(self.gp.X, self.gp.Y, 'kx', ms=10, mew=3)
            # self.gp.plot(plot_limits=np.array(self.bounds).squeeze(),
            #              ax=axis)

    def add_new_data_point(self, x, y):
        """
        Add a new function observation to the GP.

        Parameters
        ----------
        x: 2d-array
        y: 2d-array
        """
        x = np.atleast_2d(x)
        y = np.atleast_2d(y)
        if self.gp is None:
            # Initialize GP
            # inference_method = GPy.inference.latent_function_inference.\
            #     exact_gaussian_inference.ExactGaussianInference()
            self.gp = GPy.core.GP(X=x, Y=y, kernel=self.kernel,
                                  # inference_method=inference_method,
                                  likelihood=self.likelihood)
        else:
            # Add data to GP
            # self.gp.set_XY(np.vstack([self.gp.X, x]),
            #                np.vstack([self.gp.Y, y]))

            # Add data row/col to kernel (a, b)
            # [ K    a ]
            # [ a.T  b ]
            #
            # Now K = L.dot(L.T)
            # The new Cholesky decomposition is then
            # L_new = [ L    0 ]
            #         [ c.T  d ]
            a = self.gp.kern.K(self.gp.X, x)
            b = self.gp.kern.K(x, x)

            b += 1e-8 + self.gp.likelihood.gaussian_variance(
                    self.gp.Y_metadata)

            L = self.gp.posterior.woodbury_chol
            c = sp.linalg.solve_triangular(self.gp.posterior.woodbury_chol, a,
                                           lower=True)

            d = np.sqrt(b - c.T.dot(c))

            L_new = np.asfortranarray(
                    np.bmat([[L, np.zeros_like(c)],
                             [c.T, d]]))

            K_new = np.bmat([[self.gp.posterior._K, a],
                             [a.T, b]])

            self.gp.X = np.vstack((self.gp.X, x))
            self.gp.Y = np.vstack((self.gp.Y, y))

            alpha, _ = dpotrs(L_new, self.gp.Y, lower=1)
            self.gp.posterior = Posterior(woodbury_chol=L_new,
                                          woodbury_vector=alpha,
                                          K=K_new)
        # Increment time step
        self.t += 1

    def remove_last_data_point(self):
        """Remove the data point that was last added to the GP."""
        # self.gp.set_XY(self.gp.X[:-1, :], self.gp.Y[:-1, :])
        self.gp.X = self.gp.X[:-1, :]
        self.gp.Y = self.gp.Y[:-1, :]
        self.gp.posterior = Posterior(
                woodbury_chol=np.asfortranarray(
                        self.gp.posterior.woodbury_chol[:-1, :-1]),
                woodbury_vector=np.asfortranarray(
                        self.gp.posterior.woodbury_vector[:-1]),
                K=self.gp.posterior._K[:-1, :-1])
        self.t -= 1


class SafeOpt(GaussianProcessOptimization):
    """
    A class to maximize a function using the adapted or original SafeOpt alg.

    Parameters
    ----------
    function: object
        A function that returns the current value that we want to optimize.
    gp: GPy Gaussian process
        A Gaussian process which is initialized with safe, initial data points.
    parameter_set: 2d-array
        List of parameters
    fmin: float
        Safety threshold for the function value
    lipschitz: float
        The Lipschitz constant of the system, if None the GP confidence
        intervals are used directly.
    beta: float or callable
        A constant or a function of the time step that scales the confidence
        interval of the acquisition function.

    """
    def __init__(self, function, gp, parameter_set, fmin,
                 lipschitz=None, beta=3.0):
        super(SafeOpt, self).__init__(function, gp, parameter_set, beta)

        self.fmin = fmin
        self.liptschitz = lipschitz

        # Value intervals
        self.C = np.empty((self.inputs.shape[0], 2), dtype=np.float)
        self.C[:] = [-np.inf, np.inf]
        self.Q = self.C.copy()

        # Safe set
        self.S = np.zeros(self.inputs.shape[0], dtype=np.bool)

        # Switch to use confidence intervals for safety
        if lipschitz is None:
            self._use_lipschitz = False
        else:
            self._use_lipschitz = True
            self.S[:self.gp.X.shape[0]] = True

        # Whether to use self-contained sets (only really needed for proof)
        self._use_contained_sets = False

        self.C[self.S, 0] = self.fmin

        # Set of expanders and maximizers
        self.G = np.zeros_like(self.S, dtype=np.bool)
        self.M = self.G.copy()

        # Update the sets
        self.update_confidence_intervals()
    @property
    def use_lipschitz(self):
        """
        Boolean that determines whether to use the Lipschitz constant.

        By default this is set to False, which means the adapted SafeOpt
        algorithm is used, that uses the GP confidence intervals directly.
        If set to True, the `self.lipschitz` parameter is used to compute
        the safe and expanders sets.
        """
        return self._use_lipschitz

    @use_lipschitz.setter
    def use_lipschitz(self, value):
        if value and self.liptschitz is None:
            raise ValueError('Lipschitz constant not defined')
        self._use_lipschitz = value

    @property
    def use_contained_sets(self):
        """
        Boolean that determines whether to use self-contained sets.

        The original SafeOpt algorithm requires self-contained predictions
        of the Gaussian process to prove theoretical results. However,
        in practice this is usually not necessary, so the this parameter
        defaults to False.
        """
        return self._use_contained_sets

    @use_contained_sets.setter
    def use_contained_sets(self, value):
        self._use_contained_sets = value

    def update_confidence_intervals(self):
        """Recompute the confidence intervals form the GP."""
        beta = self.beta(self.t)

        # Evaluate acquisition function
        mean, var = self.gp._raw_predict(self.inputs)
        mean = mean.squeeze()
        std_dev = np.sqrt(var.squeeze())

        # Update confidence intervals
        self.Q[:, 0] = mean - beta * std_dev
        self.Q[:, 1] = mean + beta * std_dev

        # Update confidence intervals if they're being used
        if self.use_contained_sets:
            # Convenient views on C and Q
            C_l, C_u = self.C.T
            Q_l, Q_u = self.Q.T

            # Update value interval, make sure C(t+1) is contained in C(t)
            self.C[:, 0] = np.where(C_l < Q_l, np.min([Q_l, C_u], 0), C_l)
            self.C[:, 1] = np.where(C_u > Q_u, np.max([Q_u, C_l], 0), C_u)

    def compute_sets(self, full_sets=False):
        """
        Compute the safe set of points, based on current confidence bounds.

        Parameters
        ----------
        full_sets: boolean
            Whether to compute the full set of expanders or whether to omit
            computations that are not relevant for running SafeOpt
            (This option is only useful for plotting purposes)
        """
        beta = self.beta(self.t)

        # Use the appropriate confidence interval
        if self.use_contained_sets:
            l, u = self.C.T
        else:
            l, u = self.Q.T

        # Expand safe set
        if self.use_lipschitz:
            # Euclidean distance between all safe and unsafe points
            # Could precompute this once for all points
            d = cdist(self.inputs[self.S], self.inputs[~self.S])

            # Apply Lipschitz constant to determine new safe points
            self.S[~self.S] = \
                np.any(l[self.S, None] - self.liptschitz * d >= self.fmin, 0)
        else:
            self.S[:] = l >= self.fmin

        if not np.any(self.S):
            raise EnvironmentError('There are no safe points to evaluate.')

        # Set of possible maximisers
        # Maximizers: safe upper bound above best, safe lower bound
        self.M[:] = False
        self.M[self.S] = u[self.S] >= np.max(l[self.S])
        max_var = np.max(u[self.M] - l[self.M])

        # Optimistic set of possible expanders
        self.G[:] = False

        # For the run of the algorithm we do not need to calculate the
        # full set of potential expanders:
        # We can skip the ones already in M and ones that have lower
        # variance than the maximum variance in M, max_var.
        # Amongst the remaining ones we only need to find the
        # potential expander with maximum variance
        if full_sets:
            s = self.S
        else:
            # skip points in M
            s = np.logical_and(self.S, ~self.M)

            # Remove points with a variance that is too small
            s[s] = u[s] - l[s] > max_var

        # no points to evalute for G, exit
        if not np.any(s):
            return

        def sort_generator(array):
            """Return the indeces of the biggest elements in order.

            Avoids sorting everything, only sort the relevant parts at a time.

            Parameters
            ----------
            array: 1d-array
                The array which we want to sort and iterate over

            Returns
            -------
            iterable:
                Indeces of the largest elements in order
            """
            sort_id = np.argpartition(array, -1)
            yield sort_id[-1]
            for i in range(1, len(array)):
                sort_id[:-i] =\
                    sort_id[:-i][np.argpartition(array[sort_id[:-i]], -1)]
                yield sort_id[-i - 1]

        # # Rather than using a generator we could just straight out sort.
        # # This is faster if we have to check more than log(n) points as
        # # expanders before finding one
        # def sort_generator(array):
        #     """Return the sorted array, largest element first."""
        #     return array.argsort()[::-1]

        # set of safe expanders
        G_safe = np.zeros(np.count_nonzero(s), dtype=np.bool)

        if not full_sets:
            # Sort, element with largest variance first
            sort_index = sort_generator(u[s] - l[s])
        else:
            # Sort index is just an enumeration of all safe states
            sort_index = range(len(G_safe))

        for index in sort_index:
            if self.use_lipschitz:
                d = cdist(self.inputs[s, :][[index], :],
                          self.inputs[~self.S, :])
                l2 = u[s][index] - self.liptschitz * d
            else:
                # Add safe point with it's max possible value to the gp
                self.add_new_data_point(self.inputs[s, :][index, :],
                                        u[s][index])

                # Prediction of unsafe points based on that
                mean2, var2 = self.gp._raw_predict(self.inputs[~self.S])

                # Remove the fake data point from the GP again
                self.remove_last_data_point()

                mean2 = mean2.squeeze()
                var2 = var2.squeeze()
                l2 = mean2 - beta * np.sqrt(var2)

            # If the unsafe lower bound is suddenly above fmin: expander
            if np.any(l2 >= self.fmin):
                G_safe[index] = True
                # Since we sorted by uncertainty and only the most
                # uncertain element gets picked by SafeOpt anyways, we can
                # stop after we found the first one
                if not full_sets:
                    break

        self.G[s] = G_safe

        # else:
        #     # Doing the same partial-prediction stuff as above is possible,
        #     # but not implemented since numpy is pretty fast anyways
        #     d = cdist(self.inputs[s], self.inputs[~self.S])
        #     self.G[s] = np.any(
        #         C_u[s, None] - self.liptschitz * d >= self.fmin, 1)

    def compute_new_query_point(self):
        """
        Computes a new point at which to evaluate the function, based on the
        sets M and G.
        """
        # Get lower and upper bounds
        if self.use_contained_sets:
            l, u = self.C.T
        else:
            l, u = self.Q.T

        MG = np.logical_or(self.M, self.G)
        value = u[MG] - l[MG]
        return self.inputs[MG][np.argmax(value)]

    def optimize(self):
        """Run one step of bayesian optimization."""
        # Update the sets of expanders/maximizers
        self.compute_sets()
        # Get new input value
        x = self.compute_new_query_point()
        # Sample noisy output
        value = self.function(x)
        # Add data point to the GP
        self.add_new_data_point(x, value)
        # Update confidence intervals based on current estimate
        self.update_confidence_intervals()

    def get_maximum(self):
        """
        Return the current estimate for the maximum.

        Returns
        -------
        x - ndarray
            Location of the maximum
        y - 0darray
            Maximum value

        """
        if self.use_contained_sets:
            l = self.C[:, 0]
        else:
            l = self.Q[:, 0]

        max_id = np.argmax(l)
        return self.inputs[max_id, :], l[max_id]
