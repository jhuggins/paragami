##########################################################################
# Functions for evaluating the sensitivity of optima to hyperparameters. #
##########################################################################

import autograd
import autograd.numpy as np
from copy import deepcopy
from math import factorial
from scipy.linalg import cho_factor, cho_solve
import warnings

from .function_patterns import FlattenedFunction


class HyperparameterSensitivityLinearApproximation:
    """
    Linearly approximate dependence of an optimum on a hyperparameter.

    Suppose we have an optimization problem in which the objective
    depends on a hyperparameter:

    .. math::

        \hat{\\theta} = \mathrm{argmin}_{\\theta} f(\\theta, \\lambda).

    The optimal parameter, :math:`\hat{\\theta}`, is a function of
    :math:`\\lambda` through the optimization problem.  In general, this
    dependence is complex and nonlinear.  To approximate this dependence,
    this class uses the linear approximation:

    .. math::

        \hat{\\theta}(\\lambda) \\approx \hat{\\theta}(\\lambda_0) +
            \\frac{d\hat{\\theta}}{d\\lambda}|_{\\lambda_0}
                (\\lambda - \\lambda_0).

    In terms of the arguments to this function,
    :math:`\\theta` corresponds to ``opt_par``,
    :math:`\\lambda` corresponds to ``hyper_par``,
    and :math:`f` corresponds to ``objective_fun``.

    Because ``opt_par`` and ``hyper_par`` in general are structured,
    constrained data, the linear approximation is evaluated in flattened
    space using user-specified patterns.

    Methods
    ------------
    set_base_values:
        Set the base values, :math:`\\lambda_0` and
        :math:`\\theta_0 := \hat\\theta(\\lambda_0)`, at which the linear
        approximation is evaluated.
    get_dopt_dhyper:
        Return the Jacobian matrix
        :math:`\\frac{d\hat{\\theta}}{d\\lambda}|_{\\lambda_0}` in flattened
        space.
    get_hessian_at_opt:
        Return the Hessian of the objective function in the
        flattened space.
    predict_opt_par_from_hyper_par:
        Use the linear approximation to predict
        the folded value of ``opt_par`` from a folded value of ``hyper_par``.
    """
    def __init__(
        self,
        objective_fun,
        opt_par_pattern, hyper_par_pattern,
        opt_par_folded_value, hyper_par_folded_value,
        opt_par_is_free, hyper_par_is_free,
        validate_optimum=True,
        hessian_at_opt=None,
        factorize_hessian=True,
        hyper_par_objective_fun=None,
        grad_tol=1e-8):
        """
        Parameters
        --------------
        objective_fun: Callable function
            A callable function, optimized by ``opt_par`` at a particular value
            of ``hyper_par``.  The function must be of the form
            ``f(folded opt_par, folded hyper_par)``.
        opt_par_pattern:
            A pattern for ``opt_par``, the optimal parameter.
        opt_par_pattern:
            A pattern for ``hyper_par``, the hyperparameter.
        opt_par_folded_value:
            The folded value of ``opt_par`` at which ``objective_fun`` is
            optimized for the given value of ``hyper_par``.
        hyper_par_folded_value:
            The folded of ``hyper_par_folded_value`` at which ``opt_par``
            optimizes ``objective_fun``.
        opt_par_is_free: Boolean
            Whether to use the free parameterization for ``opt_par`` when
            linearzing.
        hyper_par_is_free: Boolean
            Whether to use the free parameterization for ``hyper_par`` when
            linearzing.
        validate_optimum: Boolean
            When setting the values of ``opt_par`` and ``hyper_par``, check
            that ``opt_par`` is, in fact, a critical point of
            ``objective_fun``.
        hessian_at_opt: Numeric matrix (optional)
            The Hessian of ``objective_fun`` at the optimum.  If not specified,
            it is calculated using automatic differentiation.
        factorize_hessian: Boolean
            If ``True``, solve the required linear system using a Cholesky
            factorization.  If ``False``, use the conjugate gradient algorithm
            to avoid forming or inverting the Hessian.
        hyper_par_objective_fun: Callable function
            A callable function of the form
            ``f(folded opt_par, folded hyper_par)`` containing the part of
            ``objective_fun`` that depends on both ``opt_par`` and
            ``hyper_par``.  If not specified, ``objective_fun`` is used.
        grad_tol: Float
            The tolerance used to check that the gradient is approximately
            zero at the optimum.
        """

        self._objective_fun = objective_fun
        self._opt_par_pattern = opt_par_pattern
        self._hyper_par_pattern = hyper_par_pattern
        self._opt_par_is_free = opt_par_is_free
        self._hyper_par_is_free = hyper_par_is_free
        self._grad_tol = grad_tol

        # Define flattened versions of the objective function and their
        # autograd derivatives.
        self._obj_fun = \
            FlattenedFunction(
                original_fun=self._objective_fun,
                patterns=[self._opt_par_pattern, self._hyper_par_pattern],
                free=[self._opt_par_is_free, self._hyper_par_is_free],
                argnums=[0, 1])
        self._obj_fun_grad = autograd.grad(self._obj_fun, argnum=0)
        self._obj_fun_hessian = autograd.hessian(self._obj_fun, argnum=0)
        self._obj_fun_hvp = autograd.hessian_vector_product(
            self._obj_fun, argnum=0)

        if hyper_par_objective_fun is None:
            self._hyper_par_objective_fun = self._objective_fun
            self._hyper_obj_fun = self._obj_fun
        else:
            self._hyper_par_objective_fun = hyper_par_objective_fun
            self._hyper_obj_fun = \
                FlattenedFunction(
                    original_fun=self._hyper_par_objective_fun,
                    patterns=[self._opt_par_pattern, self._hyper_par_pattern],
                    free=[self._opt_par_is_free, self._hyper_par_is_free],
                    argnums=[0, 1])

        # TODO: is this the right default order?  Make this flexible.
        self._hyper_obj_fun_grad = autograd.grad(self._hyper_obj_fun, argnum=0)
        self._hyper_obj_cross_hess = autograd.jacobian(
            self._hyper_obj_fun_grad, argnum=1)

        self.set_base_values(
            opt_par_folded_value, hyper_par_folded_value,
            hessian_at_opt, factorize_hessian, validate=validate_optimum)

    def set_base_values(self,
                        opt_par_folded_value, hyper_par_folded_value,
                        hessian_at_opt, factorize_hessian,
                        validate=True, grad_tol=None):
        if grad_tol is None:
            grad_tol = self._grad_tol

        # Set the values of the optimal parameters.
        self._opt0 = self._opt_par_pattern.flatten(
            opt_par_folded_value, free=self._opt_par_is_free)
        self._hyper0 = self._hyper_par_pattern.flatten(
            hyper_par_folded_value, free=self._hyper_par_is_free)

        if validate:
            # Check that the gradient of the objective is zero at the optimum.
            grad0 = self._obj_fun_grad(self._opt0, self._hyper0)
            grad0_norm = np.linalg.norm(grad0)
            if np.linalg.norm(grad0) > grad_tol:
                err_msg = \
                    'The gradient is not zero at the putatively optimal ' + \
                    'values.  ||grad|| = {} > {} = grad_tol'.format(
                        grad0_norm, grad_tol)
                raise ValueError(err_msg)

        # Set the values of the Hessian at the optimum.
        self._factorize_hessian = factorize_hessian
        if self._factorize_hessian:
            if hessian_at_opt is None:
                self._hess0 = self._obj_fun_hessian(self._opt0, self._hyper0)
            else:
                self._hess0 = hessian_at_opt
            self._hess0_chol = cho_factor(self._hess0)
        else:
            if hessian_at_opt is not None:
                raise ValueError('If factorize_hessian is False, ' +
                                 'hessian_at_opt must be None.')
            self._hess0 = None
            self._hess0_chol = None

        self._cross_hess = self._hyper_obj_cross_hess(self._opt0, self._hyper0)
        self._sens_mat = -1 * cho_solve(self._hess0_chol, self._cross_hess)

    # Methods:
    def get_dopt_dhyper(self):
        return self._sens_mat

    def get_hessian_at_opt(self):
        return self._hess0

    def predict_opt_par_from_hyper_par(self, new_hyper_par_folded_value,
                                       fold_output=True):
        """
        Predict ``opt_par`` using the linear approximation.

        Parameters
        ------------
        new_hyper_par_folded_value:
            The folded value of ``hyper_par`` at which to approximate
            ``opt_par``.
        fold_output: Boolean
            Whether to return ``opt_par`` as a folded value.  If ``False``,
            returns the flattened value according to ``opt_par_pattern``
            and ``opt_par_is_free``.
        """

        if not self._factorize_hessian:
            raise NotImplementedError(
                'CG is not yet implemented for predict_opt_par_from_hyper_par')

        hyper1 = self._hyper_par_pattern.flatten(
            new_hyper_par_folded_value, free=self._hyper_par_is_free)
        opt_par1 = self._opt0 + self._sens_mat @ (hyper1 - self._hyper0)
        if fold_output:
            return self._opt_par_pattern.fold(
                opt_par1, free=self._opt_par_is_free)
        else:
            return opt_par1


################################
# Higher-order approximations. #
################################

def _append_jvp(fun, num_base_args=1, argnum=0):
    """
    Append a jacobian vector product to a function.

    This function is designed to be used recursively to calculate
    higher-order Jacobian-vector products.

    Parameters
    --------------
    fun: Callable function
        The function to be differentiated.
    num_base_args: integer
        The number of inputs to the base function, i.e.,
        to the function before any differentiation.
     argnum: inteeger
        Which argument should be differentiated with respect to.
        Must be between 0 and num_base_args - 1.

    Returns
    ------------
    Denote the base args x1, ..., xB, where B == num_base_args.
    Let argnum = k.  Then _append_jvp returns a function,
    fun_jvp(x1, ..., xB, ..., v) =
    \sum_i (dfun_dx_{ki}) v_i | (x1, ..., xB).
    That is, it returns the Jacobian vector product where the Jacobian
    is taken with respect to xk, and the vector product is with the
    final argument.
    """
    assert argnum < num_base_args

    fun_jvp = autograd.make_jvp(fun, argnum=argnum)
    def obj_jvp_wrapper(*argv):
        # These are the base arguments -- the points at which the
        # Jacobians are evaluated.
        base_args = argv[0:num_base_args]

        # The rest of the arguments are the vectors, with which inner
        # products are taken in the order they were passed to
        # _append_jvp.
        vec_args = argv[num_base_args:]

        if (len(vec_args) > 1):
            # Then this is being applied to an existing Jacobian
            # vector product.  The last will be the new vector, and
            # we need to evaluate the function at the previous vectors.
            # The new jvp will be appended to the end of the existing
            # list.
            old_vec_args = vec_args[:-1]
            return fun_jvp(*base_args, *old_vec_args)(vec_args[-1])[1]
        else:
            return fun_jvp(*base_args)(*vec_args)[1]

    return obj_jvp_wrapper


class DerivativeTerm:
    """
    A single term in a Taylor expansion of a two-parameter objective with
    methods for computing its derivatives.

    .. note::
        This class is intended for internal use.  Most users should not
        use ``DerivativeTerm`` directly, and should rather use
        ``ParametricSensitivityTaylorExpansion``.

    Let :math:`\hat{\\eta}(\\epsilon)` be such that
    :math:`g(\hat{\\eta}(\\epsilon), \\epsilon) = 0`.
    The nomenclature assumes that
    the term arises from calculating total derivatives of
    :math:`g(\hat{\\eta}(\\epsilon), \\epsilon)`,
    with respect to :math:`\\epsilon`, so such a term arose from repeated
    applications of the chain and product rule of differentiation with respect
    to :math:`\\epsilon`.

    In the ``ParametricSensitivityTaylorExpansion`` class, such terms are
    then used to calculate

    .. math::
        \\frac{d^k\hat{\\eta}}{d\\epsilon^k} |_{\\eta_0, \\epsilon_0}.

    We assume the term will only be calculated summed against a single value
    of :math:`\\Delta\\epsilon`, so we do not need to keep track of the
    order in which the derivatives are evaluated.

    Every term arising from differentiation of :math:`g(\hat{\\eta}(\\epsilon),
    \\epsilon)` with respect to :math:`\\epsilon` is a product the following
    types of terms.

    First, there are the partial derivatives of :math:`g` itself.

    .. math::
        \\frac{\\partial^{m+n} g(\\eta, \\epsilon)}
              {\\partial \\eta^m \\epsilon^n}

    In the preceding display, ``m``
    is the total number of :math:`\\eta` derivatives, i.e.
    ``m = np.sum(eta_orders)``, and ``n = eps_order``.

    Each partial derivative of :math:`g` with respect to :math:`\\epsilon`
    will multiply one :math:`\\Delta \\epsilon` term directly.  Each
    partial derivative with respect to :math:`\\eta` will multiply a term
    of the form

    .. math::
        \\frac{d^p \hat{\\eta}}{d \\epsilon^p}

    which will in turn multiply :math:`p` different :math:`\\Delta \\epsilon`
    terms. The number of such terms of order :math:`p` are given by the entry
    ``eta_orders[p - 1]``.  Each such terms arises from a single partial
    derivative of :math:`g` with respect to :math:`\\eta`, which is why
    the above ``m = np.sum(eta_orders)``.

    Finally, the term is multiplied by the constant ``prefactor``.

    For example, suppose that ``eta_orders = [1, 0, 2]``, ``prefactor = 1.5``,
    and ``epsilon_order = 2``.  Then the derivative term is

    .. math::
        1.5 \\cdot
        \\frac{\\partial^{5} g(\hat{\\eta}, \\epsilon)}
              {\\partial \\eta^3 \\epsilon^2} \\cdot
        \\frac{d \hat{\\eta}}{d \\epsilon} \\cdot
        \\frac{d^3 \hat{\\eta}}{d \\epsilon^3} \\cdot
        \\frac{d^3 \hat{\\eta}}{d \\epsilon^3} \\cdot

    ...which will multiply a total of
    ``9 = epsilon_order + np.sum(eta_orders * [1, 2, 3])``
    :math:`\\Delta \\epsilon` terms.  Such a term would arise in
    the 9-th order Taylor expansion of :math:`g(\hat{\\eta}(\\epsilon),
    \\epsilon)` in :math:`\\epsilon`.

    Attributes
    -----------------
    eps_order:
        The total number of epsilon derivatives of g.
    eta_orders:
        A vector of length order - 1.  Entry i contains the number
        of terms d\eta^{i + 1} / d\epsilon^{i + 1}.
    prefactor:
        The constant multiple in front of this term.

    Methods
    ------------
    evaluate:
        Get the value of the current derivative term.
    differentiate:
        Get a list of derivatives terms resulting from differentiating this
        term.
    check_similarity:
        Return a boolean indicating whether this term is equivalent to another
        term in the order of its derivative.
    combine_with:
        Return the sum of this term and another term.
    """
    def __init__(self, eps_order, eta_orders, prefactor,
                 eval_eta_derivs, eval_g_derivs):
        """
        Parameters
        -------------
        eps_order:
            The total number of epsilon derivatives of g.
        eta_orders:
            A vector of length order - 1.  Entry i contains the number
            of terms :math:`d\\eta^{i + 1} / d\\epsilon^{i + 1}`.
        prefactor:
            The constant multiple in front of this term.
        eval_eta_derivs:
            A vector of functions to evaluate :math:`d\\eta^i / d\\epsilon^i`.
            The functions should take arguments (eta0, eps0, deps) and the
            i-th entry should evaluate
            :math:`d\\eta^i / d\\epsilon^i (d \\epsilon^i) |_{\\eta_0, \\epsilon_0}`.
        eval_g_derivs:
            A list of lists of g jacobian vector product functions.
            The array should be such that
            eval_g_derivs[i][j](eta0, eps0, v1 ... vi, w1 ... wj)
            evaluates d^{i + j} G / (deta^i)(deps^j)(v1 ... vi)(w1 ... wj).
        """
        # Base properties.
        self.eps_order = eps_order
        self.eta_orders = eta_orders
        self.prefactor = prefactor
        self._eval_eta_derivs = eval_eta_derivs
        self._eval_g_derivs = eval_g_derivs

        # Derived quantities.

        # The order is the total number of epsilon derivatives.
        self._order = int(
            self.eps_order + \
            np.sum(self.eta_orders * np.arange(1, len(self.eta_orders) + 1)))

        # The derivative of g needed for this particular term.
        self.eval_g_deriv = \
            eval_g_derivs[np.sum(eta_orders)][self.eps_order]

        # Sanity checks.
        # The rules of differentiation require that these assertions be true
        # -- that is, if terms are generated using the differentiate()
        # method from other well-defined terms, these assertions should always
        # be sastisfied.
        assert isinstance(self.eps_order, int)
        assert len(self.eta_orders) == self._order
        assert self.eps_order >= 0 # Redundant
        for eta_order in self.eta_orders:
            assert eta_order >= 0
            assert isinstance(eta_order, int)
        assert len(self._eval_eta_derivs) >= self._order - 1
        assert len(eval_g_derivs) > len(self.eta_orders)
        for eta_deriv_list in eval_g_derivs:
            assert len(eta_deriv_list) > self.eps_order

    def __str__(self):
        return 'Order: {}\t{} * eta{} * eps[{}]'.format(
            self._order, self.prefactor, self.eta_orders, self.eps_order)

    def evaluate(self, eta0, eps0, deps):
        # First eta arguments, then epsilons.
        vec_args = []

        for i in range(len(self.eta_orders)):
            eta_order = self.eta_orders[i]
            if eta_order > 0:
                vec = self._eval_eta_derivs[i](eta0, eps0, deps)
                for j in range(eta_order):
                    vec_args.append(vec)

        for i in range(self.eps_order):
            vec_args.append(deps)

        return self.prefactor * self.eval_g_deriv(eta0, eps0, *vec_args)

    def differentiate(self, eval_next_eta_deriv):
        derivative_terms = []
        new_eval_eta_derivs = deepcopy(self._eval_eta_derivs)
        new_eval_eta_derivs.append(eval_next_eta_deriv)

        old_eta_orders = deepcopy(self.eta_orders)
        old_eta_orders.append(0)

        # dG / deps.
        derivative_terms.append(
            DerivativeTerm(
                eps_order=self.eps_order + 1,
                eta_orders=deepcopy(old_eta_orders),
                prefactor=self.prefactor,
                eval_eta_derivs=new_eval_eta_derivs,
                eval_g_derivs=self._eval_g_derivs))

        # dG / deta.
        new_eta_orders = deepcopy(old_eta_orders)
        new_eta_orders[0] = new_eta_orders[0] + 1
        derivative_terms.append(
            DerivativeTerm(
                eps_order=self.eps_order,
                eta_orders=new_eta_orders,
                prefactor=self.prefactor,
                eval_eta_derivs=new_eval_eta_derivs,
                eval_g_derivs=self._eval_g_derivs))

        # Derivatives of each d^{i}eta / deps^i term.
        for i in range(len(self.eta_orders)):
            eta_order = self.eta_orders[i]
            if eta_order > 0:
                new_eta_orders = deepcopy(old_eta_orders)
                new_eta_orders[i] = new_eta_orders[i] - 1
                new_eta_orders[i + 1] = new_eta_orders[i + 1] + 1
                derivative_terms.append(
                    DerivativeTerm(
                        eps_order=self.eps_order,
                        eta_orders=new_eta_orders,
                        prefactor=self.prefactor * eta_order,
                        eval_eta_derivs=new_eval_eta_derivs,
                        eval_g_derivs=self._eval_g_derivs))

        return derivative_terms

    # Return whether another term matches this one in the pattern of derivatives.
    def check_similarity(self, term):
        return \
            (self.eps_order == term.eps_order) & \
            (self.eta_orders == term.eta_orders)

    # Assert that another term has the same pattern of derivatives and
    # return a new term that combines the two.
    def combine_with(self, term):
        assert self.check_similarity(term)
        return DerivativeTerm(
            eps_order=self.eps_order,
            eta_orders=self.eta_orders,
            prefactor=self.prefactor + term.prefactor,
            eval_eta_derivs=self._eval_eta_derivs,
            eval_g_derivs=self._eval_g_derivs)


def _generate_two_term_fwd_derivative_array(fun, order):
    """
    Generate an array of JVPs of the two arguments of the target function fun.

    Parameters
    -------------
    fun: callable function
        The function to be differentiated.  The first two arguments
        should be vectors for differentiation, i.e., fun should have signature
        fun(x1, x2, ...) and return a numeric value.
     order: integer
        The maximum order of the derivative to be generated.

    Returns
    ------------
    An array of functions where element eval_fun_derivs[i][j] is a function
    ``eval_fun_derivs[i][j](x1, x2, ..., v1, ... vi, w1, ..., wj)) =
    d^{i + j}fun / (dx1^i dx2^j) v1 ... vi w1 ... wj``.
    """
    eval_fun_derivs = [[ fun ]]
    for x1_ind in range(order):
        if x1_ind > 0:
            # Append one x1 derivative.
            next_deriv = _append_jvp(
                eval_fun_derivs[x1_ind - 1][0], num_base_args=2, argnum=0)
            eval_fun_derivs.append([ next_deriv ])
        for x2_ind in range(order):
            # Append one x2 derivative.
            next_deriv = _append_jvp(
                eval_fun_derivs[x1_ind][x2_ind], num_base_args=2, argnum=1)
            eval_fun_derivs[x1_ind].append(next_deriv)
    return eval_fun_derivs



def _consolidate_terms(dterms):
    """
    Combine like derivative terms.

    Arguments
    -----------
    dterms:
        A list of DerivativeTerms.

    Returns
    ------------
    A new list of derivative terms that evaluate equivalently where
    terms with the same derivative signature have been combined.
    """
    unmatched_indices = [ ind for ind in range(len(dterms)) ]
    consolidated_dterms = []
    while len(unmatched_indices) > 0:
        match_term = dterms[unmatched_indices.pop(0)]
        for ind in unmatched_indices:
            if (match_term.eta_orders == dterms[ind].eta_orders):
                match_term = match_term.combine_with(dterms[ind])
                unmatched_indices.remove(ind)
        consolidated_dterms.append(match_term)

    return consolidated_dterms


def evaluate_terms(dterms, eta0, eps0, deps, include_highest_eta_order=True):
    """
    Evaluate a list of derivative terms.

    Parameters
    ---------------
    dterms:
        A list of derivative terms.
    eta0:
        The value of the first argument at which the derivative is evaluated.
    eps0:
        The value of the second argument at which the derivative is evaluated.
    deps: numpy array
        The change in epsilon by which to multiply the Jacobians.
    include_highest_eta_order: boolean
        If true, include the term with
        ``d^k eta / deps^k``, where ``k == order``.  The main use of these
        DerivativeTerms at the time of writing is precisely to evaluate this
        term using the other terms, and this can be accomplished by setting
        include_highest_eta_order to False.

    Returns
    ---------------
        The sum of the evaluated DerivativeTerms.
    """
    vec = None
    for term in dterms:
        if include_highest_eta_order or (term.eta_orders[-1] == 0):
            if vec is None:
                vec = term.evaluate(eta0, eps0, deps)
            else:
                vec += term.evaluate(eta0, eps0, deps)
    return vec


# Get the terms to start a Taylor expansion.
def _get_taylor_base_terms(eval_g_derivs):
    dterms1 = [ \
        DerivativeTerm(
            eps_order=1,
            eta_orders=[0],
            prefactor=1.0,
            eval_eta_derivs=[],
            eval_g_derivs=eval_g_derivs),
        DerivativeTerm(
            eps_order=0,
            eta_orders=[1],
            prefactor=1.0,
            eval_eta_derivs=[],
            eval_g_derivs=eval_g_derivs) ]
    return dterms1


# Given a collection of dterms (formed either with _get_taylor_base_terms
# or derivatives), evaluate the implied dketa_depsk.
#
# Args:
#   - hess0: The Hessian of the objective wrt the first argument.
#   - dterms: An array of DerivativeTerms.
#   - eta0: The value of the first argument.
#   - eps0: The value of the second argument.
#   - deps: The change in epsilon by which to multiply the Jacobians.
def evaluate_dketa_depsk(hess0, dterms, eta0, eps0, deps):
    vec = evaluate_terms(
        dterms, eta0, eps0, deps, include_highest_eta_order=False)
    assert vec is not None
    return -1 * np.linalg.solve(hess0, vec)


# Calculate the derivative of an array of DerivativeTerms.
#
# Args:
#   - hess0: The Hessian of the objective wrt the first argument.
#   - dterms: An array of DerivativeTerms.
#
# Returns:
#   An array of the derivatives of dterms with respect to the second argument.
def differentiate_terms(hess0, dterms):
    def eval_next_eta_deriv(eta, eps, deps):
        return evaluate_dketa_depsk(hess0, dterms, eta, eps, deps)

    dterms_derivs = []
    for term in dterms:
        dterms_derivs += term.differentiate(eval_next_eta_deriv)
    return _consolidate_terms(dterms_derivs)
    return dterms_derivs



class ParametricSensitivityTaylorExpansion(object):
    """
    Evaluate the Taylor series of an optimum on a hyperparameter.

    This is a class for computing the Taylor series of
    eta(eps) = argmax_eta objective(eta, eps) using forward-mode automatic
    differentation.

    .. note:: This class is experimental and should be used with caution.

    Methods
    --------------
    evaluate_dkinput_dhyperk:
        Evaluate the k-th derivative.
    evaluate_taylor_series:
        Evaluate the Taylor series.
    """
    def __init__(self, objective_function,
                 input_val0, hyper_val0, order,
                 hess0=None, hyper_par_objective_function=None):
        """
        Parameters
        ------------------
        objective_function: callable function
            The optimization objective as a function of two arguments
            (eta, eps), where eta is the parameter that is optimized and
            eps is a hyperparameter.
        input_val0: numpy array
            The value of ``input_par`` at the optimum.
        hyper_val0: numpy array
            The value of ``hyper_par`` at which ``input_val0`` was found.
        order: positive integer
            The maximum order of the Taylor series to be calculated.
        hess0: numpy array
            Optional.  The Hessian of the objective at
            (``input_val0``, ``hyper_val0``).
            If not specified it is calculated at initialization.
        hyper_par_objective_function:
            Optional.  A function containing the dependence
            of ``objective_function`` on the hyperparameter.  Sometimes
            only a small, easily calculated part of the objective depends
            on the hyperparameter, and by specifying
            ``hyper_par_objective_function`` the
            necessary calculations can be more efficient.  If
            unset, ``objective_function`` is used.
        """
        warnings.warn(
            'The ParametricSensitivityTaylorExpansion is experimental.')
        self._objective_function = objective_function
        self._objective_function_hessian = \
            autograd.hessian(self._objective_function, argnum=0)

        # In order to calculate derivatives d^kinput_dhyper^k, we will be
        # Taylor expanding the gradient of the objective with respect to eta.
        self._objective_function_eta_grad = \
            autograd.grad(self._objective_function, argnum=0)

        if hyper_par_objective_function is None:
            self._hyper_par_objective_function = self._objective_function
        else:
            self._hyper_par_objective_function = hyper_par_objective_function

        self.set_base_values(input_val0, hyper_val0)
        self._set_order(order)

    def set_base_values(self, input_val0, hyper_val0, hess0=None):
        """
        Set the values at which the Taylor series is to be evaluated.

        Parameters
        ---------------
        input_val0: numpy array
            The value of input_par at the optimum.
        hyper_val0: numpy array
            The value of hyper_par at which input_val0 was found.
        hess0: numpy array
            Optional.  The Hessian of the objective at (input_val0, hyper_val0).
            If not specified it is calculated at initialization.
        """
        self._input_val0 = deepcopy(input_val0)
        self._hyper_val0 = deepcopy(hyper_val0)

        if hess0 is None:
            self._hess0 = \
                self._objective_function_hessian(
                    self._input_val0, self._hyper_val0)
        else:
            self._hess0 = hess0
        self._hess0_chol = cho_factor(self._hess0)

    # Get a function returning the next derivative from the Taylor terms dterms.
    def _get_dkinput_dhyperk_from_terms(self, dterms):
        def dkinput_dhyperk(input_val, hyper_val, dhyper, tolerance=1e-8):
            if tolerance is not None:
                # Make sure you're evaluating sensitivity at the base parameters.
                assert np.max(np.abs(input_val - self._input_val0)) <= tolerance
                assert np.max(np.abs(hyper_val - self._hyper_val0)) <= tolerance
            return evaluate_dketa_depsk(
                self._hess0, dterms,
                self._input_val0, self._hyper_val0, dhyper)
        return dkinput_dhyperk

    def _differentiate_terms(self, dterms, eval_next_eta_deriv):
        dterms_derivs = []
        for term in dterms:
            dterms_derivs += term.differentiate(eval_next_eta_deriv)
        return _consolidate_terms(dterms_derivs)

    def _set_order(self, order):
        self._order = order

        # You need one more gradient derivative than the order of the Taylor
        # approximation.
        self._eval_g_derivs = _generate_two_term_fwd_derivative_array(
            self._objective_function_eta_grad, order=self._order + 1)

        self._taylor_terms_list = \
            [ _get_taylor_base_terms(self._eval_g_derivs) ]
        self._dkinput_dhyperk_list = []
        for k in range(self._order - 1):
            next_dkinput_dhyperk = \
                self._get_dkinput_dhyperk_from_terms(
                    self._taylor_terms_list[k])
            next_taylor_terms = \
                self._differentiate_terms(
                    self._taylor_terms_list[k], next_dkinput_dhyperk)
            self._dkinput_dhyperk_list.append(next_dkinput_dhyperk)
            self._taylor_terms_list.append(next_taylor_terms)

        self._dkinput_dhyperk_list.append(
            self._get_dkinput_dhyperk_from_terms(
                self._taylor_terms_list[self._order - 1]))

    def evaluate_dkinput_dhyperk(self, dhyper, k):
        """
        Evaluate the derivative d^k input / d hyper^k in the direction dhyper.

        Parameters
        --------------
        dhyper: numpy array
            The direction (hyper_val - hyper_val0).
        k: integer
            The order of the derivative.

        Returns
        ------------
            The value of the k^th derivative in the directoin dhyper.
        """
        if k <= 0:
            raise ValueError('k must be at least one.')
        if k > self._order:
            raise ValueError(
                'k must be no greater than the declared order={}'.format(
                    self._order))
        deriv_fun = self._dkinput_dhyperk_list[k - 1]
        return deriv_fun(self._input_val0, self._hyper_val0, dhyper)

    def evaluate_taylor_series(self, dhyper, add_offset=True, max_order=None):
        """
        Evaluate the derivative ``d^k input / d hyper^k`` in the direction dhyper.

        Parameters
        --------------
        dhyper: numpy array
            The direction (hyper_val - hyper_val0).
        add_offset: boolean
            Optional.  Whether to add the initial constant input_val0 to the
            Taylor series.
        max_order: integer
            Optional.  The order of the Taylor series.  Defaults to the
            ``order`` argument to ``__init__``.

        Returns
        ------------
            The Taylor series approximation to ``input_vak(hyper_val)`` if
            ``add_offset`` is ``True``, or to
            ``input_val(hyper_val) - input_val0`` if ``False``.
        """
        if max_order is None:
            max_order = self._order
        if max_order <= 0:
            raise ValueError('max_order must be greater than zero.')
        if max_order > self._order:
            raise ValueError(
                'max_order must be no greater than the declared order={}'.format(
                    self._order))

        dinput = 0
        for k in range(1, max_order + 1):
            dinput += self.evaluate_dkinput_dhyperk(dhyper, k) / \
                float(factorial(k))

        if add_offset:
            return dinput + self._input_val0
        else:
            return dinput

    def print_terms(self, k=None):
        """
        Print the derivative terms in the Taylor series.

        Parameters
        ---------------
        k: integer
            Optional.  Which term to print.  If unspecified, all terms are
            printed.
        """
        if k is not None and k > self._order:
            raise ValueError(
                'k must be no greater than order={}'.format(self._order))
        for order in range(self._order):
            if k is None or order == (k - 1):
                print('\nTerms for order {}:'.format(order + 1))
                for term in self._taylor_terms_list[order]:
                    print(term)