"""Bayesian Gaussian Mixture Models and
Dirichlet Process Gaussian Mixture Models"""

# Author: Alexandre Passos (alexandre.tp@gmail.com)
#         Bertrand Thirion <bertrand.thirion@inria.fr>
#
# Based on mixture.py by:
#         Ron Weiss <ronweiss@gmail.com>
#         Fabian Pedregosa <fabian.pedregosa@inria.fr>
#

import numpy as np
import warnings
from scipy.special import digamma as _digamma, gammaln as _gammaln
from scipy import linalg
from scipy.spatial.distance import cdist

from ..utils import check_random_state
from ..utils.extmath import norm
from ..utils import deprecated
from .. import cluster
from .gmm import GMM


def sqnorm(v):
    return norm(v) ** 2


def digamma(x):
    return _digamma(x + np.finfo(np.float32).eps)


def gammaln(x):
    return _gammaln(x + np.finfo(np.float32).eps)


def log_normalize(v, axis=0):
    """Normalized probabilities from unnormalized log-probabilites"""
    v = np.rollaxis(v, axis)
    v = v.copy()
    v -= v.max(axis=0)
    out = np.log(np.sum(np.exp(v), axis=0))
    v = np.exp(v - out)
    v += np.finfo(np.float32).eps
    v /= np.sum(v, axis=0)
    return np.swapaxes(v, 0, axis)


def wishart_log_det(a, b, detB, n_features):
    """Expected value of the log of the determinant of a Wishart

    The expected value of the logarithm of the determinant of a
    wishart-distributed random variable with the specified parameters."""
    l = np.sum(digamma(0.5 * (a - np.arange(-1, n_features - 1))))
    l += n_features * np.log(2)
    return l + detB


def wishart_logz(v, s, dets, n_features):
    "The logarithm of the normalization constant for the wishart distribution"
    z = 0.
    z += 0.5 * v * n_features * np.log(2)
    z += (0.25 * (n_features * (n_features - 1)) * np.log(np.pi))
    z += 0.5 * v * np.log(dets)
    z += np.sum(gammaln(0.5 * (v - np.arange(n_features) + 1)))
    return z


def _bound_wishart(a, B, detB):
    """Returns a function of the dof, scale matrix and its determinant
    used as an upper bound in variational approcimation of the evidence"""
    n_features = B.shape[0]
    logprior = wishart_logz(a, B, detB, n_features)
    logprior -= wishart_logz(n_features,
                             np.identity(n_features),
                             1, n_features)
    logprior += 0.5 * (a - 1) * wishart_log_det(a, B, detB, n_features)
    logprior += 0.5 * a * np.trace(B)
    return logprior


##############################################################################
# Variational bound on the log likelihood of each class
##############################################################################


def _sym_quad_form(x, mu, A):
    """helper function to calculate symmetric quadratic form x.T * A * x"""
    q = (cdist(x, mu[np.newaxis], "mahalanobis", VI=A) ** 2).reshape(-1)
    return q


def _bound_state_log_lik(X, initial_bound, precs, means, covariance_type):
    """Update the bound with likelihood terms, for standard covariance types"""
    n_components, n_features = means.shape
    n_samples = X.shape[0]
    bound = np.empty((n_samples, n_components))
    bound[:] = initial_bound
    if covariance_type in ['diag', 'spherical']:
        for k in xrange(n_components):
            d = X - means[k]
            bound[:, k] -= 0.5 * np.sum(d * d * precs[k], axis=1)
    elif covariance_type == 'tied':
        for k in xrange(n_components):
            bound[:, k] -= 0.5 * _sym_quad_form(X, means[k], precs)
    elif covariance_type == 'full':
        for k in xrange(n_components):
            bound[:, k] -= 0.5 * _sym_quad_form(X, means[k], precs[k])
    return bound


class DPGMM(GMM):
    """Variational Inference for the Infinite Gaussian Mixture Model.

    DPGMM stands for Dirichlet Process Gaussian Mixture Model, and it
    is an infinite mixture model with the Dirichlet Process as a prior
    distribution on the number of clusters. In practice the
    approximate inference algorithm uses a truncated distribution with
    a fixed maximum number of components, but almost always the number
    of components actually used depends on the data.

    Stick-breaking Representation of a Gaussian mixture model
    probability distribution. This class allows for easy and efficient
    inference of an approximate posterior distribution over the
    parameters of a Gaussian mixture model with a variable number of
    components (smaller than the truncation parameter n_components).

    Initialization is with normally-distributed means and identity
    covariance, for proper convergence.

    Parameters
    ----------
    n_components: int, optional
        Number of mixture components. Defaults to 1.

    covariance_type: string, optional
        String describing the type of covariance parameters to
        use.  Must be one of 'spherical', 'tied', 'diag', 'full'.
        Defaults to 'diag'.

    alpha: float, optional
        Real number representing the concentration parameter of
        the dirichlet process. Intuitively, the Dirichlet Process
        is as likely to start a new cluster for a point as it is
        to add that point to a cluster with alpha elements. A
        higher alpha means more clusters, as the expected number
        of clusters is ``alpha*log(N)``. Defaults to 1.

    thresh : float, optional
        Convergence threshold.

    Attributes
    ----------
    covariance_type : string
        String describing the type of covariance parameters used by
        the DP-GMM.  Must be one of 'spherical', 'tied', 'diag', 'full'.

    n_components : int
        Number of mixture components.

    `weights_` : array, shape (`n_components`,)
        Mixing weights for each mixture component.

    `means_` : array, shape (`n_components`, `n_features`)
        Mean parameters for each mixture component.

    `precisions_` : array
        Precision (inverse covariance) parameters for each mixture
        component.  The shape depends on `covariance_type`::

            (`n_components`, 'n_features')                if 'spherical',
            (`n_features`, `n_features`)                  if 'tied',
            (`n_components`, `n_features`)                if 'diag',
            (`n_components`, `n_features`, `n_features`)  if 'full'

    `converged_` : bool
        True when convergence was reached in fit(), False otherwise.

    See Also
    --------
    GMM : Finite Gaussian mixture model fit with EM

    VBGMM : Finite Gaussian mixture model fit with a variational
    algorithm, better for situations where there might be too little
    data to get a good estimate of the covariance matrix.
    """

    def __init__(self, n_components=1, covariance_type='diag', alpha=1.0,
                 random_state=None, thresh=1e-2, verbose=False,
                 min_covar=None, n_iter=10, params='wmc', init_params='wmc'):
        self.alpha = alpha
        self.verbose = verbose
        super(DPGMM, self).__init__(n_components, covariance_type,
                                    random_state=random_state,
                                    thresh=thresh, min_covar=min_covar,
                                    n_iter=n_iter, params=params, 
                                    init_params=init_params)

    def _get_precisions(self):
        """Return precisions as a full matrix."""
        if self._covariance_type == 'full':
            return self.precs_
        elif self._covariance_type in ['diag', 'spherical']:
            return [np.diag(cov) for cov in self.precs_]
        elif self._covariance_type == 'tied':
            return [self.precs_] * self.n_components

    def _get_covars(self):
        return [linalg.pinv(c) for c in self._get_precisions()]

    def _set_covars(self, covars):
        raise NotImplementedError("""The variational algorithm does
        not support setting the covariance parameters.""")

    def eval(self, X):
        """Evaluate the model on data

        Compute the bound on log probability of X under the model
        and return the posterior distribution (responsibilities) of
        each mixture component for each element of X.

        This is done by computing the parameters for the mean-field of
        z for each observation.

        Parameters
        ----------
        X : array_like, shape (n_samples, n_features)
            List of n_features-dimensional data points.  Each row
            corresponds to a single data point.

        Returns
        -------
        logprob : array_like, shape (n_samples,)
            Log probabilities of each data point in X
        responsibilities: array_like, shape (n_samples, n_components)
            Posterior probabilities of each mixture component for each
            observation
        """
        X = np.asarray(X)
        if X.ndim == 1:
            X = X[:, np.newaxis]
        z = np.zeros((X.shape[0], self.n_components))
        sd = digamma(self.gamma_.T[1] + self.gamma_.T[2])
        dgamma1 = digamma(self.gamma_.T[1]) - sd
        dgamma2 = np.zeros(self.n_components)
        dgamma2[0] = digamma(self.gamma_[0, 2]) - digamma(self.gamma_[0, 1] +
                                                          self.gamma_[0, 2])
        for j in xrange(1, self.n_components):
            dgamma2[j] = dgamma2[j - 1] + digamma(self.gamma_[j - 1, 2])
            dgamma2[j] -= sd[j - 1]
        dgamma = dgamma1 + dgamma2
        # Free memory and developers cognitive load:
        del dgamma1, dgamma2, sd

        if self._covariance_type not in ['full', 'tied', 'diag', 'spherical']:
            raise NotImplementedError("This ctype is not implemented: %s"
                                      % self._covariance_type)
        p = _bound_state_log_lik(X, self._initial_bound + self.bound_prec_,
                                 self.precs_, self.means_,
                                 self._covariance_type)
        z = p + dgamma
        z = log_normalize(z, axis=-1)
        bound = np.sum(z * p, axis=-1)
        return bound, z

    def _update_concentration(self, z):
        """Update the concentration parameters for each cluster"""
        sz = np.sum(z, axis=0)
        self.gamma_.T[1] = 1. + sz
        self.gamma_.T[2].fill(0)
        for i in xrange(self.n_components - 2, -1, -1):
            self.gamma_[i, 2] = self.gamma_[i + 1, 2] + sz[i]
        self.gamma_.T[2] += self.alpha

    def _update_means(self, X, z):
        """Update the variational distributions for the means"""
        n_features = X.shape[1]
        for k in xrange(self.n_components):
            if self._covariance_type in ['spherical', 'diag']:
                num = np.sum(z.T[k].reshape((-1, 1)) * X, axis=0)
                num *= self.precs_[k]
                den = 1. + self.precs_[k] * np.sum(z.T[k])
                self.means_[k] = num / den
            elif self._covariance_type in ['tied', 'full']:
                if self._covariance_type == 'tied':
                    cov = self.precs_
                else:
                    cov = self.precs_[k]
                den = np.identity(n_features) + cov * np.sum(z.T[k])
                num = np.sum(z.T[k].reshape((-1, 1)) * X, axis=0)
                num = np.dot(cov, num)
                self.means_[k] = linalg.lstsq(den, num)[0]

    def _update_precisions(self, X, z):
        """Update the variational distributions for the precisions"""
        n_features = X.shape[1]
        if self._covariance_type == 'spherical':
            self.dof_ = 0.5 * n_features * np.sum(z, axis=0)
            for k in xrange(self.n_components):
                # could be more memory efficient ?
                sq_diff = np.sum((X - self.means_[k]) ** 2, axis=1)
                self.scale_[k] = 1.
                self.scale_[k] += 0.5 * np.sum(z.T[k] * (sq_diff + n_features))
                self.bound_prec_[k] = (
                    0.5 * n_features * (
                        digamma(self.dof_[k]) - np.log(self.scale_[k])))
            self.precs_ = np.tile(self.dof_ / self.scale_, [n_features, 1]).T

        elif self._covariance_type == 'diag':
            for k in xrange(self.n_components):
                self.dof_[k].fill(1. + 0.5 * np.sum(z.T[k], axis=0))
                sq_diff = (X - self.means_[k]) ** 2  # see comment above
                self.scale_[k] = np.ones(n_features) + 0.5 * np.dot(
                    z.T[k], (sq_diff + 1))
                self.precs_[k] = self.dof_[k] / self.scale_[k]
                self.bound_prec_[k] = 0.5 * np.sum(digamma(self.dof_[k])
                                                    - np.log(self.scale_[k]))
                self.bound_prec_[k] -= 0.5 * np.sum(self.precs_[k])

        elif self._covariance_type == 'tied':
            self.dof_ = 2 + X.shape[0] + n_features
            self.scale_ = (X.shape[0] + 1) * np.identity(n_features)
            for k in xrange(self.n_components):
                    diff = X - self.means_[k]
                    self.scale_ += np.dot(diff.T, z[:, k:k + 1] * diff)
            self.scale_ = linalg.pinv(self.scale_)
            self.precs_ = self.dof_ * self.scale_
            self.det_scale_ = linalg.det(self.scale_)
            self.bound_prec_ = 0.5 * wishart_log_det(
                self.dof_, self.scale_, self.det_scale_, n_features)
            self.bound_prec_ -= 0.5 * self.dof_ * np.trace(self.scale_)

        elif self._covariance_type == 'full':
            for k in xrange(self.n_components):
                sum_resp = np.sum(z.T[k])
                self.dof_[k] = 2 + sum_resp + n_features
                self.scale_[k] = (sum_resp + 1) * np.identity(n_features)
                diff = X - self.means_[k]
                self.scale_[k] += np.dot(diff.T, z[:, k:k + 1] * diff)
                self.scale_[k] = linalg.pinv(self.scale_[k])
                self.precs_[k] = self.dof_[k] * self.scale_[k]
                self.det_scale_[k] = linalg.det(self.scale_[k])
                self.bound_prec_[k] = 0.5 * wishart_log_det(self.dof_[k],
                                                            self.scale_[k],
                                                            self.det_scale_[k],
                                                           n_features)
                self.bound_prec_[k] -= 0.5 * self.dof_[k] * np.trace(
                    self.scale_[k])

    def _monitor(self, X, z, n, end=False):
        """Monitor the lower bound during iteration

        Debug method to help see exactly when it is failing to converge as
        expected.

        Note: this is very expensive and should not be used by default."""
        if self.verbose:
            print "Bound after updating %8s: %f" % (n, self.lower_bound(X, z))
            if end == True:
                print "Cluster proportions:", self.gamma_.T[1]
                print "covariance_type:", self._covariance_type

    def _do_mstep(self, X, z, params):
        """Maximize the variational lower bound

        Update each of the parameters to maximize the lower bound."""
        self._monitor(X, z, "z")
        self._update_concentration(z)
        self._monitor(X, z, "gamma")
        if 'm' in params:
            self._update_means(X, z)
        self._monitor(X, z, "mu")
        if 'c' in params:
            self._update_precisions(X, z)
        self._monitor(X, z, "a and b", end=True)

    def _initialize_gamma(self):
        "Initializes the concentration parameters"
        self.gamma_ = self.alpha * np.ones((self.n_components, 3))

    def _bound_concentration(self):
        """The variational lower bound for the concentration parameter."""
        logprior = gammaln(self.alpha) * self.n_components
        logprior += np.sum((self.alpha - 1) * (
                digamma(self.gamma_.T[2]) - digamma(self.gamma_.T[1] +
                                                    self.gamma_.T[2])))
        logprior += np.sum(- gammaln(self.gamma_.T[1] + self.gamma_.T[2]))
        logprior += np.sum(gammaln(self.gamma_.T[1]) +
                           gammaln(self.gamma_.T[2]))
        logprior -= np.sum((self.gamma_.T[1] - 1) * (
                digamma(self.gamma_.T[1]) - digamma(self.gamma_.T[1] +
                                                     self.gamma_.T[2])))
        logprior -= np.sum((self.gamma_.T[2] - 1) * (
                digamma(self.gamma_.T[2]) - digamma(self.gamma_.T[1] +
                                                    self.gamma_.T[2])))
        return logprior

    def _bound_means(self):
        "The variational lower bound for the mean parameters"
        logprior = 0.
        logprior -= 0.5 * sqnorm(self.means_)
        logprior -= 0.5 * self.means_.shape[1] * self.n_components
        return logprior

    def _bound_precisions(self):
        """Returns the bound term related to precisions"""
        logprior = 0.
        if self._covariance_type == 'spherical':
            logprior += np.sum(gammaln(self.dof_))
            logprior -= np.sum(
                (self.dof_ - 1) * digamma(np.maximum(0.5, self.dof_)))
            logprior += np.sum(- np.log(self.scale_) + self.dof_ -\
                                     self.precs_[:, 0])
        elif self._covariance_type == 'diag':
            logprior += np.sum(gammaln(self.dof_))
            logprior -= np.sum(
                (self.dof_ - 1) * digamma(np.maximum(0.5, self.dof_)))
            logprior += np.sum(- np.log(self.scale_) + self.dof_ - self.precs_)
        elif self._covariance_type == 'tied':
            logprior += _bound_wishart(self.dof_, self.scale_, self.det_scale_)
        elif self._covariance_type == 'full':
            for k in xrange(self.n_components):
                logprior += _bound_wishart(self.dof_[k],
                                           self.scale_[k],
                                           self.det_scale_[k])
        return logprior

    def _bound_proportions(self, z):
        """Returns the bound term related to proportions"""
        dg12 = digamma(self.gamma_.T[1] + self.gamma_.T[2])
        dg1 = digamma(self.gamma_.T[1]) - dg12
        dg2 = digamma(self.gamma_.T[2]) - dg12

        cz = np.cumsum(z[:, ::-1], axis=-1)[:, -2::-1]
        logprior = np.sum(cz * dg2[:-1]) + np.sum(z * dg1)
        del cz  # Save memory
        z_non_zeros = z[z > np.finfo(np.float32).eps]
        logprior -= np.sum(z_non_zeros * np.log(z_non_zeros))
        return logprior

    def _logprior(self, z):
        logprior = self._bound_concentration()
        logprior += self._bound_means()
        logprior += self._bound_precisions()
        logprior += self._bound_proportions(z)
        return logprior

    def lower_bound(self, X, z):
        """returns a lower bound on model evidence based on X and membership"""
        if self._covariance_type not in ['full', 'tied', 'diag', 'spherical']:
            raise NotImplementedError("This ctype is not implemented: %s"
                                      % self._covariance_type)

        X = np.asarray(X)
        if X.ndim == 1:
            X = X[:, np.newaxis]
        c = np.sum(z * _bound_state_log_lik(
                X, self._initial_bound + self.bound_prec_,
                self.precs_, self.means_, self._covariance_type))

        return c + self._logprior(z)

    def fit(self, X, **kwargs):
        """Estimate model parameters with the variational
        algorithm.

        For a full derivation and description of the algorithm see
        doc/dp-derivation/dp-derivation.tex

        A initialization step is performed before entering the em
        algorithm. If you want to avoid this step, set the keyword
        argument init_params to the empty string ''. Likewise, if you
        would like just to do an initialization, call this method with
        n_iter=0.

        Parameters
        ----------
        X : array_like, shape (n, n_features)
            List of n_features-dimensional data points.  Each row
            corresponds to a single data point.
        n_iter : int, optional
             Maximum number of iterations to perform before convergence.
        params : string, optional
            Controls which parameters are updated in the training
            process.  Can contain any combination of 'w' for weights,
            'm' for means, and 'c' for covars.  Defaults to 'wmc'.
        init_params : string, optional
            Controls which parameters are updated in the initialization
            process.  Can contain any combination of 'w' for weights,
            'm' for means, and 'c' for covars.  Defaults to 'wmc'.
        """
        self.random_state = check_random_state(self.random_state)
        if kwargs:
            warnings.warn("Setting parameters in the 'fit' method is deprecated"
                     "Set it on initialization instead.",
                     DeprecationWarning)
            # initialisations for in case the user still adds parameters to fit
            # so things don't break
            if 'n_iter' in kwargs:
                self.n_iter =  kwargs['n_iter']
            if 'params' in kwargs:
                self.params = kwargs['params']            
            if 'init_params' in kwargs:
                self.init_params = kwargs['init_params']

        ## initialization step
        X = np.asarray(X)
        if X.ndim == 1:
            X = X[:, np.newaxis]

        n_features = X.shape[1]
        z = np.ones((X.shape[0], self.n_components))
        z /= self.n_components

        self._initial_bound = - 0.5 * n_features * np.log(2 * np.pi)
        self._initial_bound -= np.log(2 * np.pi * np.e)

        if (self.init_params != '') or not hasattr(self, 'gamma_'):
            self._initialize_gamma()

        if 'm' in self.init_params or not hasattr(self, 'means_'):
            self.means_ = cluster.KMeans(
                k=self.n_components,
                random_state=self.random_state).fit(X).cluster_centers_[::-1]

        if 'w' in self.init_params or not hasattr(self, 'weights_'):
            self.weights_ = np.tile(1.0 / self.n_components, self.n_components)

        if 'c' in self.init_params or not hasattr(self, 'precs_'):
            if self._covariance_type == 'spherical':
                self.dof_ = np.ones(self.n_components)
                self.scale_ = np.ones(self.n_components)
                self.precs_ = np.ones((self.n_components, n_features))
                self.bound_prec_ = 0.5 * n_features * (
                    digamma(self.dof_) - np.log(self.scale_))
            elif self._covariance_type == 'diag':
                self.dof_ = 1 + 0.5 * n_features
                self.dof_ *= np.ones((self.n_components, n_features))
                self.scale_ = np.ones((self.n_components, n_features))
                self.precs_ = np.ones((self.n_components, n_features))
                self.bound_prec_ = 0.5 * (np.sum(digamma(self.dof_) -
                                                 np.log(self.scale_), 1))
                self.bound_prec_ -= 0.5 * np.sum(self.precs_, 1)
            elif self._covariance_type == 'tied':
                self.dof_ = 1.
                self.scale_ = np.identity(n_features)
                self.precs_ = np.identity(n_features)
                self.det_scale_ = 1.
                self.bound_prec_ = 0.5 * wishart_log_det(
                    self.dof_, self.scale_, self.det_scale_, n_features)
                self.bound_prec_ -= 0.5 * self.dof_ * np.trace(self.scale_)
            elif self._covariance_type == 'full':
                self.dof_ = (1 + self.n_components + X.shape[0])
                self.dof_ *= np.ones(self.n_components)
                self.scale_ = [2 * np.identity(n_features)
                           for i in xrange(self.n_components)]
                self.precs_ = [np.identity(n_features)
                                for i in xrange(self.n_components)]
                self.det_scale_ = np.ones(self.n_components)
                self.bound_prec_ = np.zeros(self.n_components)
                for k in xrange(self.n_components):
                    self.bound_prec_[k] = wishart_log_det(
                        self.dof_[k], self.scale_[k], self.det_scale_[k],
                        n_features)
                    self.bound_prec_[k] -= (self.dof_[k] *
                                            np.trace(self.scale_[k]))
                self.bound_prec_ *= 0.5

        logprob = []
        # reset self.converged_ to False
        self.converged_ = False
        for i in xrange(self.n_iter):
            # Expectation step
            curr_logprob, z = self.eval(X)
            logprob.append(curr_logprob.sum() + self._logprior(z))

            # Check for convergence.
            if i > 0 and abs(logprob[-1] - logprob[-2]) < self.thresh:
                self.converged_ = True
                break

            # Maximization step
            self._do_mstep(X, z, self.params)

        return self


class VBGMM(DPGMM):
    """Variational Inference for the Gaussian Mixture Model

    Variational inference for a Gaussian mixture model probability
    distribution. This class allows for easy and efficient inference
    of an approximate posterior distribution over the parameters of a
    Gaussian mixture model with a fixed number of components.

    Initialization is with normally-distributed means and identity
    covariance, for proper convergence.

    Parameters
    ----------
    n_components: int, optional
        Number of mixture components. Defaults to 1.

    covariance_type: string, optional
        String describing the type of covariance parameters to
        use.  Must be one of 'spherical', 'tied', 'diag', 'full'.
        Defaults to 'diag'.

    alpha: float, optional
        Real number representing the concentration parameter of
        the dirichlet distribution. Intuitively, the higher the
        value of alpha the more likely the variational mixture of
        Gaussians model will use all components it can. Defaults
        to 1.


    Attributes
    ----------
    covariance_type : string
        String describing the type of covariance parameters used by
        the DP-GMM.  Must be one of 'spherical', 'tied', 'diag', 'full'.

    n_features : int
        Dimensionality of the Gaussians.

    n_components : int (read-only)
        Number of mixture components.

    `weights_` : array, shape (`n_components`,)
        Mixing weights for each mixture component.

    `means_` : array, shape (`n_components`, `n_features`)
        Mean parameters for each mixture component.

    `precisions_` : array
        Precision (inverse covariance) parameters for each mixture
        component.  The shape depends on `covariance_type`::

            (`n_components`, 'n_features')                if 'spherical',
            (`n_features`, `n_features`)                  if 'tied',
            (`n_components`, `n_features`)                if 'diag',
            (`n_components`, `n_features`, `n_features`)  if 'full'

    `converged_` : bool
        True when convergence was reached in fit(), False
        otherwise.

    See Also
    --------
    GMM : Finite Gaussian mixture model fit with EM
    DPGMM : Ininite Gaussian mixture model, using the dirichlet
    process, fit with a variational algorithm
    """

    def __init__(self, n_components=1, covariance_type='diag', alpha=1.0,
                 random_state=None, thresh=1e-2, verbose=False,
                 min_covar=None, n_iter=10, params='wmc', init_params='wmc'):
        super(VBGMM, self).__init__(
            n_components, covariance_type, random_state=random_state,
            thresh=thresh, verbose=verbose, min_covar=min_covar,
            n_iter=n_iter, params=params, init_params=init_params)
        self.alpha = float(alpha) / n_components

    def eval(self, X):
        """Evaluate the model on data

        Compute the bound on log probability of X under the model
        and return the posterior distribution (responsibilities) of
        each mixture component for each element of X.

        This is done by computing the parameters for the mean-field of
        z for each observation.

        Parameters
        ----------
        X : array_like, shape (n_samples, n_features)
            List of n_features-dimensional data points.  Each row
            corresponds to a single data point.

        Returns
        -------
        logprob : array_like, shape (n_samples,)
            Log probabilities of each data point in X
        responsibilities: array_like, shape (n_samples, n_components)
            Posterior probabilities of each mixture component for each
            observation
        """
        X = np.asarray(X)
        if X.ndim == 1:
            X = X[:, np.newaxis]
        z = np.zeros((X.shape[0], self.n_components))
        p = np.zeros(self.n_components)
        bound = np.zeros(X.shape[0])
        dg = digamma(self.gamma_) - digamma(np.sum(self.gamma_))

        if self._covariance_type not in ['full', 'tied', 'diag', 'spherical']:
            raise NotImplementedError("This ctype is not implemented: %s"
                                      % self._covariance_type)
        p = _bound_state_log_lik(
                X, self._initial_bound + self.bound_prec_,
                self.precs_, self.means_, self._covariance_type)

        z = p + dg
        z = log_normalize(z, axis=-1)
        bound = np.sum(z * p, axis=-1)
        return bound, z

    def _update_concentration(self, z):
        for i in xrange(self.n_components):
            self.gamma_[i] = self.alpha + np.sum(z.T[i])

    def _initialize_gamma(self):
        self.gamma_ = self.alpha * np.ones(self.n_components)

    def _bound_proportions(self, z):
        logprior = 0.
        dg = digamma(self.gamma_)
        dg -= digamma(np.sum(self.gamma_))
        logprior += np.sum(dg.reshape((-1, 1)) * z.T)
        z_non_zeros = z[z > np.finfo(np.float32).eps]
        logprior -= np.sum(z_non_zeros * np.log(z_non_zeros))
        return logprior

    def _bound_concentration(self):
        logprior = 0.
        logprior = gammaln(np.sum(self.gamma_)) - gammaln(self.n_components
                                                          * self.alpha)
        logprior -= np.sum(gammaln(self.gamma_) - gammaln(self.alpha))
        sg = digamma(np.sum(self.gamma_))
        logprior += np.sum((self.gamma_ - self.alpha)
                           * (digamma(self.gamma_) - sg))
        return logprior

    def _monitor(self, X, z, n, end=False):
        """Monitor the lower bound during iteration

        Debug method to help see exactly when it is failing to converge as
        expected.

        Note: this is very expensive and should not be used by default."""
        if self.verbose:
            print "Bound after updating %8s: %f" % (n, self.lower_bound(X, z))
            if end == True:
                print "Cluster proportions:", self.gamma_
                print "covariance_type:", self._covariance_type
