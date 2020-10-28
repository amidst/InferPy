import numpy as np
import functools

import math
import tensorflow as tf

from inferpy import contextmanager
from inferpy import util
from inferpy.data.preprocess import to_numpy, add_sample_dim, create_batches




def flatten_result(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        simplify_result = kwargs.pop('simplify_result', True)
        result = f(*args, **kwargs)
        if simplify_result and len(result) == 1:
            return result[list(result.keys())[0]]
        else:
            return result
    return wrapper


class Query:
    def __init__(self, variables, target_names=None, data={}, enable_interceptor_variables=(None, None)):
        # enable_interceptor_variables is a tuple to intercept global and local hidden variables independently
        # if provided a single name, create a list with only one item
        if isinstance(target_names, str):
            target_names = [target_names]

        # raise an error if target_names is not None and contains variable names not in variables
        if target_names and any((name not in variables for name in target_names)):
            raise ValueError("Target names must correspond to variable names")

        self.target_variables = variables if not target_names else \
            {k: v for k, v in variables.items() if k in target_names}

        self.query_vars = variables
        self.enable_interceptor_variables = enable_interceptor_variables

        vars_datamodel = dict([(k,v) for (k,v) in self.query_vars.items() if v.is_datamodel])

        # all observations from vars in the data model must have the sample dimension
        self.data = add_sample_dim(to_numpy(data), vars_datamodel)

        # the batch size should be equal to the plateau in the model
        self.batch_size = list(vars_datamodel.values())[0].shape[0].value if len(vars_datamodel)>0 else 1

        # observed variables in the datamodel
        obs_datamodel = [k for (k,v) in self.query_vars.items() if k in self.data.keys() and v.is_datamodel]

        # actual data size
        if len(obs_datamodel)>0:
            d = self.data[obs_datamodel[0]]
            if isinstance(d, tf.Tensor):
                self.data_size = d.get_shape().as_list()[0]
            else:
                self.data_size = d.shape[0]
        else:
            self.data_size = self.batch_size

        # get batches (even if we have a single one)
        self.batches = create_batches(self.data, vars_datamodel, self.data_size, self.batch_size)







    @flatten_result
    @util.tf_run_ignored
    def log_prob(self):
        """ Computes the log probabilities of a (set of) sample(s)"""

        results = []
        for batch in self.batches:
            with util.interceptor.enable_interceptor(*self.enable_interceptor_variables):
                with contextmanager.observe(self.query_vars, batch):
                    result = util.runtime.try_run({k: v.log_prob(v.value) for k, v in self.target_variables.items()})

            results.append(result)

        return self.process_output(results)


    def sum_log_prob(self):
        """ Computes the sum of the log probabilities (evaluated) of a (set of) sample(s)"""
        # The decorator is not needed here because this function returns a single value
        return np.sum([np.mean(lp) for lp in self.log_prob(simplify_result=False).values()])


    @flatten_result
    @util.tf_run_ignored
    def sample(self, size=1):
        """ Generates a sample for eache variable in the model """
        results = []
        for batch in self.batches:
            with util.interceptor.enable_interceptor(*self.enable_interceptor_variables):
                with contextmanager.observe(self.query_vars, batch):
                    # each iteration for `size` run the dict in the session, so if there are dependencies among random vars
                    # they are computed in the same graph operations, and reflected in the results
                    samples = [util.runtime.try_run(self.target_variables) for _ in range(size)]

            if size == 1:
                result = samples[0]
            else:
                # compact all samples in one single dict
                result = {k: np.array([sample[k] for sample in samples]) for k in self.target_variables.keys()}

            results.append(result)

        return self.process_output(results)


    def process_output(self, results):
        out = {}
        for r in results:
            for k,v in self.target_variables.items():
                if k not in out:
                    out[k] = r[k]
                elif v.is_datamodel:
                    # merge batches
                    out[k] = np.concatenate([out[k], r[k]], axis=-2)
                    if self.data_size<out[k].shape[-2]:
                        out[k] = np.take(out[k], range(self.data_size), axis=-2)

        return out



    @flatten_result
    @util.tf_run_ignored
    def parameters(self, names=None):
        """ Return the parameters of the Random Variables of the model.
        If `names` is None, then return all the parameters of all the Random Variables.
        If `names` is a list, then return the parameters specified in the list (if exists) for all the Random Variables.
        If `names` is a dict, then return all the parameters specified (value) for each Random Variable (key).

        Note:
            If `tf_run=True`, but any of the returned parameters is not a Tensor and therefore cannot be evaluated)
            this returns a not evaluated dict (because the evaluation will raise an Exception)

        Args:
            names: A list, a dict or None. Specify the parameters for the Random Variables to be obtained.

        Returns:
            A dict, where the keys are the names of the Random Variables and the values a dict of parameters (name-value)

        """
        # argument type checking
        if not(names is None or isinstance(names, (list, dict))):
            raise TypeError("The argument 'names' must be None, a list or a dict, not {}.".format(type(names)))
        # now we can assume that names is None, a list or a dict

        # function to filter the parameters for each Random Variable
        def filter_parameters(varname, parameters):
            parameter_names = list(parameters.keys())
            if names is None:
                # use all the parameters
                selected_parameters = parameter_names
            else:
                # filter by names; if is a dict and key not in, use all the parameters
                selected_parameters = set(names if isinstance(names, list) else names.get(varname, parameters))

            return {k: util.runtime.try_run(v) for k, v in parameters.items() if k in selected_parameters}

        # function that merges the parameters of 2 batches
        def merge_params(p1, p2):
            out = {}
            var = self.query_vars[p1["name"]]
            for k,v in p1.items():
                if np.ndim(v) == 0 or  \
                        (var.is_datamodel and len(var.sample_shape.as_list())>0) or \
                        not var.is_datamodel:
                    out[k] = v
                else:
                    out[k] = np.vstack([p1[k], p2[k]])[0:self.data_size]
            return out

        # get the parameter for each batch
        result = {}

        for batch in self.batches:
            with contextmanager.observe(self.query_vars, batch):
                r = {k: filter_parameters(k, v.parameters)
                          for k, v in self.target_variables.items()}

            if len(result.keys()) == 0:
                result = r
            else:
                result = {var:merge_params(result[var], r[var]) for var in self.target_variables.keys()}
        return result
