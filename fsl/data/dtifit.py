#!/usr/bin/env python
#
# dtifit.py - The DTIFitTensor class, and some related utility functions.
#
# Author: Paul McCarthy <pauldmccarthy@gmail.com>
# Author: Michiel Cottaar <michiel.cottaar@ndcn.ox.ac.uk>
#
"""This module provides the :class:`.DTIFitTensor` class, which encapsulates
the diffusion tensor data generated by the FSL ``dtifit`` tool.

There are also conversion tools between the diffusion tensors defined in 3 formats:

* (..., 3, 3) array with the full diffusion tensor
* (..., 6) array with the unique components (Dxx, Dxy, Dxz, Dyy, Dyz, Dzz)
* Tuple with the eigenvectors and eigenvalues (V1, V2, V3, L1, L2, L3)


Finally the following utility functions are also defined:

  .. autosummary::
     :nosignatures:

     getDTIFitDataPrefix
     isDTIFitPath
     looksLikeTensorImage
     decomposeTensorMatrix
"""


import                 logging
import                 re
import                 glob
import os.path      as op

import numpy        as np
import numpy.linalg as npla

from . import image as fslimage


log = logging.getLogger(__name__)


def eigendecompositionToTensor(V1, V2, V3, L1, L2, L3):
    """
    Converts the eigenvalues/eigenvectors into a 3x3 diffusion tensor

    :param V1: (..., 3) shaped array with the first eigenvector
    :param V2: (..., 3) shaped array with the second eigenvector
    :param V3: (..., 3) shaped array with the third eigenvector
    :param L1: (..., ) shaped array with the first eigenvalue
    :param L2: (..., ) shaped array with the second eigenvalue
    :param L3: (..., ) shaped array with the third eigenvalue
    :return: (..., 3, 3) array with the diffusion tensor
    """
    check_shape = L1.shape
    for eigen_value in (L2, L3):
        if eigen_value.shape != check_shape:
            raise ValueError("Not all eigenvalues have the same shape")
    for eigen_vector in (V1, V2, V3):
        if eigen_vector.shape != eigen_value.shape + (3, ):
            raise ValueError("Not all eigenvectors have the same shape as the eigenvalues")
    return (
        L1[..., None, None] * V1[..., None, :] * V1[..., :, None] +
        L2[..., None, None] * V2[..., None, :] * V2[..., :, None] +
        L3[..., None, None] * V3[..., None, :] * V3[..., :, None]
    )


def tensorToEigendecomposition(matrices):
    """
    Decomposes the 3x3 diffusion tensor into eigenvalues and eigenvectors

    :param matrices: (..., 3, 3) array-like with diffusion tensor
    :return: Tuple containing the eigenvectors and eigenvalues (V1, V2, V3, L1, L2, L3)
    """
    matrices = np.asanyarray(matrices)
    if matrices.shape[-2:] != (3, 3):
        raise ValueError("Expected 3x3 diffusion tensors")

    shape = matrices.shape[:-2]
    nvoxels = np.prod(shape)

    # Calculate the eigenvectors and
    # values on all of those matrices
    flat_matrices = matrices.reshape((-1, 3, 3))
    vals, vecs = npla.eigh(flat_matrices)
    vecShape   = shape + (3, )

    l1 = vals[:, 2]   .reshape(shape)
    l2 = vals[:, 1]   .reshape(shape)
    l3 = vals[:, 0]   .reshape(shape)
    v1 = vecs[:, :, 2].reshape(vecShape)
    v2 = vecs[:, :, 1].reshape(vecShape)
    v3 = vecs[:, :, 0].reshape(vecShape)
    return v1, v2, v3, l1, l2, l3


def tensorToComponents(matrices):
    """
    Extracts the 6 unique components from a 3x3 diffusion tensor

    :param matrices: (..., 3, 3) array-like with diffusion tensors
    :return: (..., 6) array with the unique components sorted like Dxx, Dxy, Dxz, Dyy, Dyz, Dzz
    """
    matrices = np.asanyarray(matrices)
    if matrices.shape[-2:] != (3, 3):
        raise ValueError("Expected 3x3 diffusion tensors")
    return np.stack([
        matrices[..., 0, 0],
        matrices[..., 0, 1],
        matrices[..., 0, 2],
        matrices[..., 1, 1],
        matrices[..., 1, 2],
        matrices[..., 2, 2],
    ], -1)


def componentsToTensor(components):
    """
    Creates 3x3 diffusion tensors from the 6 unique components

    :param components: (..., 6) array-like with Dxx, Dxy, Dxz, Dyy, Dyz, Dzz
    :return: (..., 3, 3) array with the diffusion tensors
    """
    components = np.asanyarray(components)
    if components.shape[-1] != 6:
        raise ValueError("Expected 6 unique components of diffusion tensor")
    first = np.stack([components[..., index] for index in (0, 1, 2)], -1)
    second = np.stack([components[..., index] for index in (1, 3, 4)], -1)
    third = np.stack([components[..., index] for index in (2, 4, 5)], -1)
    return np.stack([first, second, third], -1)


def eigendecompositionToComponents(V1, V2, V3, L1, L2, L3):
    """
    Converts the eigenvalues/eigenvectors into the 6 unique components of the diffusion tensor

    :param V1: (..., 3) shaped array with the first eigenvector
    :param V2: (..., 3) shaped array with the second eigenvector
    :param V3: (..., 3) shaped array with the third eigenvector
    :param L1: (..., ) shaped array with the first eigenvalue
    :param L2: (..., ) shaped array with the second eigenvalue
    :param L3: (..., ) shaped array with the third eigenvalue
    :return: (..., 6) array with the unique components sorted like Dxx, Dxy, Dxz, Dyy, Dyz, Dzz
    """
    return tensorToComponents(eigendecompositionToTensor(V1, V2, V3, L1, L2, L3))


def componentsToEigendecomposition(components):
    """
    Decomposes diffusion tensor defined by its 6 unique components

    :param components: (..., 6) array-like with Dxx, Dxy, Dxz, Dyy, Dyz, Dzz
    :return: Tuple containing the eigenvectors and eigenvalues (V1, V2, V3, L1, L2, L3)
    """
    return tensorToEigendecomposition(componentsToTensor(components))



def getDTIFitDataPrefix(path):
    """Returns the prefix (a.k,a, base name) used for the ``dtifit`` file
    names in the given directory, or ``None`` if the ``dtifit`` files could
    not be identified.
    """

    v1s   = glob.glob(op.join(path, '*_V1.*'))
    v2s   = glob.glob(op.join(path, '*_V2.*'))
    v3s   = glob.glob(op.join(path, '*_V3.*'))
    l1s   = glob.glob(op.join(path, '*_L1.*'))
    l2s   = glob.glob(op.join(path, '*_L2.*'))
    l3s   = glob.glob(op.join(path, '*_L3.*'))
    files = [v1s, v2s, v3s, l1s, l2s, l3s]

    # Gather all of the existing file
    # prefixes into a dictionary of
    # prefix : [file list] mappings.
    pattern  = r'^(.*)_(?:V1|V2|V3|L1|L2|L3).*$'
    prefixes = {}

    for f in [f for flist in files for f in flist]:
        prefix = re.findall(pattern, f)[0]

        if prefix not in prefixes: prefixes[prefix] = [f]
        else:                      prefixes[prefix].append(f)

    # Discard any prefixes which are
    # not present for every file type.
    for prefix, files in list(prefixes.items()):
        if len(files) != 6:
            prefixes.pop(prefix)

    # Discard any prefixes which
    # match any files that do
    # not look like image files
    for prefix, files in list(prefixes.items()):
        if not all([fslimage.looksLikeImage(f) for f in files]):
            prefixes.pop(prefix)

    prefixes = list(prefixes.keys())

    # No more prefixes remaining -
    # this is probably not a dtifit
    # directory
    if len(prefixes) == 0:
        return None

    # If there's more than one remaining
    # prefix, I don't know what to do -
    # just return the first one.
    if len(prefixes) > 1:
        log.warning('Multiple dtifit prefixes detected: {}'.format(prefixes))

    return op.basename(sorted(prefixes)[0])


def isDTIFitPath(path):
    """Returns ``True`` if the given directory path looks like it contains
    ``dtifit`` data, ``False`` otherwise.
    """

    return getDTIFitDataPrefix(path) is not None


def looksLikeTensorImage(image):
    """Returns ``True`` if the given :class:`.Image` looks like it could
    contain tensor matrix data, ``False`` otherwise.
    """

    return len(image.shape) == 4 and image.shape[3] == 6


def decomposeTensorMatrix(data):
    """Decomposes the given ``numpy`` array into six separate arrays,
    containing the eigenvectors and eigenvalues of the tensor matrix
    decompositions.

    :arg image: A 4D ``numpy`` array with 6 volumes, which contains
                the unique elements of diffusion tensor matrices at
                every voxel.

    :returns:   A tuple containing the principal eigenvectors and
                eigenvalues of the tensor matrix.
    """
    return componentsToEigendecomposition(data)


class DTIFitTensor(fslimage.Nifti):
    """The ``DTIFitTensor`` class is able to load and encapsulate the diffusion
    tensor data generated by the FSL ``dtifit`` tool.  The ``DtiFitTensor``
    class supports tensor model data generated by ``dtifit``, where the
    eigenvectors and eigenvalues of the tensor matrices have been saved as six
    separate NIFTI images.
    """


    def __init__(self, path):
        """Create a ``DTIFitTensor``.

        :arg path: A path to a ``dtifit`` directory.
        """

        prefix      = getDTIFitDataPrefix(path)
        isDTIfitDir = prefix is not None

        if not isDTIfitDir:
            raise ValueError('{} does not look like a dtifit '
                             'output directory!'.format(path))

        # DTIFit output directory with separate
        # eigenvector/eigenvalue images

        self.__v1 = fslimage.Image(op.join(path, '{}_V1'.format(prefix)))
        self.__v2 = fslimage.Image(op.join(path, '{}_V2'.format(prefix)))
        self.__v3 = fslimage.Image(op.join(path, '{}_V3'.format(prefix)))
        self.__l1 = fslimage.Image(op.join(path, '{}_L1'.format(prefix)))
        self.__l2 = fslimage.Image(op.join(path, '{}_L2'.format(prefix)))
        self.__l3 = fslimage.Image(op.join(path, '{}_L3'.format(prefix)))

        fslimage.Nifti.__init__(self, self.__l1.header)

        self.dataSource = op.abspath(path)
        self.name       = '{}'.format(op.basename(path))

    def V1(self):
        return self.__v1
    def V2(self):
        return self.__v2
    def V3(self):
        return self.__v3
    def L1(self):
        return self.__l1
    def L2(self):
        return self.__l2
    def L3(self):
        return self.__l3