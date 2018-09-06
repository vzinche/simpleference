from __future__ import print_function
import os
import json

import numpy as np
import dask
import toolz as tz
import functools

from .io import IoN5, IoHDF5  # IoDVID


def load_input(io, offset, context, output_shape, padding_mode='reflect'):
    shape = io.shape
    if len(shape)==4: 
        has_channels=True
        shape=shape[1:]
        
    starts = [off - context[i] for i, off in enumerate(offset)]
    stops = [off + output_shape[i] + context[i] for i, off in enumerate(offset)]

    # we pad the input volume if necessary
    pad_left = None
    pad_right = None

    # check for padding to the left
    if any(start < 0 for start in starts):
        pad_left = tuple(abs(start) if start < 0 else 0 for start in starts)
        starts = [max(0, start) for start in starts]

    # check for padding to the right
    if any(stop > shape[i] for i, stop in enumerate(stops)):
        pad_right = tuple(stop - shape[i] if stop > shape[i] else 0 for i, stop in enumerate(stops))
        stops = [min(shape[i], stop) for i, stop in enumerate(stops)]

    bb = tuple(slice(start, stop) for start, stop in zip(starts, stops))
    if has_channels: bb = (slice(0, None), ) + bb
    data = io.read(bb)

    # pad if necessary
    if pad_left is not None or pad_right is not None:
        pad_left = (0, 0, 0) if pad_left is None else pad_left
        pad_right = (0, 0, 0) if pad_right is None else pad_right
        pad_width = tuple((pl, pr) for pl, pr in zip(pad_left, pad_right))
        if has_channels: pad_width = ((0,0),) + pad_width
        data = np.pad(data, pad_width, mode=padding_mode)  

    return data


def run_inference_n5(prediction,
                     preprocess,
                     postprocess,
                     raw_path,
                     save_file,
                     offset_list,
                     input_shape,
                     output_shape,
                     input_key,
                     target_keys,
                     padding_mode='reflect',
                     num_cpus=5,
                     log_processed=None,
                     channel_order=None):

    assert os.path.exists(raw_path)
    assert os.path.exists(raw_path)
    assert os.path.exists(save_file)
    if isinstance(target_keys, str):
        target_keys = (target_keys,)
    # The N5 IO/Wrapper needs iterables as keys
    # so we wrap the input key in a list.
    # Note that this is not the case for the hdf5 wrapper,
    # which just takes a single key.
    io_in = IoN5(raw_path, [input_key])

    io_out = IoN5(save_file, target_keys, channel_order=channel_order)
    run_inference(prediction, preprocess, postprocess, io_in, io_out,
                  offset_list, input_shape, output_shape, padding_mode=padding_mode,
                  num_cpus=num_cpus, log_processed=log_processed)
    # This is not necessary for n5 datasets
    # which do not need to be closed, but we leave it here for
    # reference when using other (hdf5) io wrappers
    io_in.close()
    io_out.close()


def run_inference_h5(prediction,
                     preprocess,
                     postprocess,
                     raw_path,
                     save_file,
                     offset_list,
                     input_shape,
                     output_shape,
                     input_key,
                     target_keys,
                     padding_mode='reflect',
                     num_cpus=5,
                     log_processed=None,
                     channel_order=None):

    assert os.path.exists(raw_path)
    assert os.path.exists(raw_path)
    assert os.path.exists(save_file)
    if isinstance(target_keys, str):
        target_keys = (target_keys,)
    # The N5 IO/Wrapper needs iterables as keys
    # so we wrap the input key in a list.
    # Note that this is not the case for the hdf5 wrapper,
    # which just takes a single key.
    io_in = IoHDF5(raw_path, [input_key])

    io_out = IoHDF5(save_file, target_keys, channel_order=channel_order)
    run_inference(prediction, preprocess, postprocess, io_in, io_out,
                  offset_list, input_shape, output_shape, padding_mode=padding_mode,
                  num_cpus=num_cpus, log_processed=log_processed)
    # This is not necessary for n5 datasets
    # which do not need to be closed, but we leave it here for
    # reference when using other (hdf5) io wrappers
    io_in.close()
    io_out.close()


def run_inference(prediction,
                  preprocess,
                  postprocess,
                  io_in,
                  io_out,
                  offset_list,
                  input_shape,
                  output_shape,
                  padding_mode='reflect',
                  num_cpus=5,
                  log_processed=None):

    assert callable(prediction)
    assert callable(preprocess)
    assert len(output_shape) == len(input_shape)

    n_blocks = len(offset_list)
    print("Starting prediction...")
    print("For %i number of blocks" % n_blocks)

    # the additional context requested in the input
    context = np.array([input_shape[i] - output_shape[i]
                        for i in range(len(input_shape))]) / 2
    context = context.astype('uint32')

    shape = io_in.shape
    if len(shape)==4: shape=shape[1:] #we have channels

    @dask.delayed
    def load_offset(offset):
        return load_input(io_in, offset, context, output_shape,
                          padding_mode=padding_mode)

    preprocess = dask.delayed(preprocess)
    predict = dask.delayed(prediction)

    if postprocess is not None:
        postprocess = dask.delayed(postprocess)

    @dask.delayed(nout=2)
    def verify_shape(offset, output):
        # crop if necessary
        stops = [off + outs for off, outs in zip(offset, output.shape[1:])]

        if any(stop > dim_size for stop, dim_size in zip(stops, shape)):
            bb = ((slice(None),) +
                  tuple(slice(0, dim_size - off if stop > dim_size else None)
                        for stop, dim_size, off in zip(stops, shape, offset)))
            output = output[bb]

        output_bounding_box = tuple(slice(off, off + outs)
                                    for off, outs in zip(offset, output_shape))
        return output, output_bounding_box

    @dask.delayed
    def write_output(output, output_bounding_box):
        io_out.write(output, output_bounding_box)
        return 1

    @dask.delayed
    def log(off):
        if log_processed is not None:
            with open(log_processed, 'a') as log_f:
                log_f.write(json.dumps(off) + ', ')
        return off

    # iterate over all the offsets, get the input data and predict
    results = []
    for offset in offset_list:
        output = tz.pipe(offset, log, load_offset, preprocess, predict)
        #print ('here output', output.shape)
        output_crop, output_bounding_box = verify_shape(offset, output)
        if postprocess is not None:
            output_crop = postprocess(output_crop, output_bounding_box)
        result = write_output(output_crop, output_bounding_box)
        results.append(result)

    get = functools.partial(dask.threaded.get, num_workers=num_cpus)
    # NOTE: Because dask.compute doesn't take an argument, but rather an
    # arbitrary number of arguments, computing each in turn, the output of
    # dask.compute(results) is a tuple of length 1, with its only element
    # being the results list. If instead we pass the results list as *args,
    # we get the desired container of results at the end.
    success = dask.compute(*results, get=get)
    print('Ran {0:} jobs'.format(sum(success)))
