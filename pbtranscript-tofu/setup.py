from setuptools import setup, find_packages
from distutils.extension import Extension
from Cython.Distutils import build_ext
import sys
import numpy as np


__author__ = "jdrake|etseng|yli@pacificbiosciences.com"
version = "0.3.tofu.20150120"

if 'setuptools.extension' in sys.modules:
    m = sys.modules['setuptools.extension']
    m.Extension.__dict__ = m._Extension.__dict__

ext_modules = [Extension("pbtools.pbtranscript.findECE",
                         ["pbtools/pbtranscript/ice/C/findECE.c"]),
                Extension("pbtools.pbtranscript.io.c_basQV",
                         ["pbtools/pbtranscript/ice/C/c_basQV.cpp"], language="c++"),
                Extension("pbtools.pbtranscript.c_Prob", 
                         ["pbtools/pbtranscript/ice/C/ProbHandler.cpp"], language="c++"),
                Extension("pbtools.pbtranscript.ice.ProbModel",
                    ["pbtools/pbtranscript/ice/C/ProbModel.cpp"], language="c++"),
                Extension("pbtools.pbtranscript.BioReaders",
                         ["pbtools/pbtranscript/io/C/BioReaders.c"]),
                Extension("pbtools.pbtranscript.c_branch", 
                         ["pbtools/pbtranscript/branch/C/c_branch.c"]), 
                Extension("pbtools.pbtranscript.modified_bx_intervals.intersection_unique", 
                         ["pbtools/pbtranscript/branch/C/modified_bx_intervals/intersection_unique.c"]),
              ]

setup(
#    setup_requires=['setuptools_cython'],
    name = 'pbtools.pbtranscript',
    version=version,
    author='Pacific Biosciences',
    author_email='devnet@pacificbiosciences.com',
    license='LICENSE.txt',
    ext_modules = ext_modules,
    include_dirs = [np.get_include()],
    scripts=['pbtools/pbtranscript/pbtranscript.py',
             'pbtools/pbtranscript/collapse_isoforms_by_sam.py',
             'pbtools/pbtranscript/fusion_finder.py',
             'pbtools/pbtranscript/tofu_wrap.py',
             'pbtools/pbtranscript/ice_partial.py',
             'pbtools/pbtranscript/ice_pbdagcon.py',
             'pbtools/pbtranscript/ice_quiver.py',
             'pbtools/pbtranscript/ice_fa2fq.py',
        ],


    entry_points={'console_scripts': [
        'fasta_splitter.py = pbtools.pbtranscript.io.FastaSplitter:main',
        'filter_sam.py = pbtools.pbtranscript.io.filter_sam:main',
        'ice_polish.py = pbtools.pbtranscript.Polish:main',
#        'ice_partial.py = pbtools.pbtranscript.ice.IcePartial:main',
#        'ice_all_partials.py = pbtools.pbtranscript.ice.IceAllPartials:main',
#        'ice_quiver.py = pbtools.pbtranscript.ice.IceQuiver:main',
        'ice_post_quiver.py = pbtools.pbtranscript.ice.IcePostQuiver:main',
        'ice_make_input_fasta_fofn.py = pbtools.pbtranscript.ice.make_input_fasta_fofn:main'
        ]},
    package_dir={'pbtools.pbtranscript': 'pbtools/pbtranscript'},
    package_data={'pbtools.pbtranscript':
                  ['data/PBMATRIX.txt',
                   'data/primers.fa',
                   'data/gcon_in.fa',
                   'data/gcon_out.fa']},
    packages=find_packages(),
    zip_safe=False,
    install_requires=[
        'pbcore >= 0.6.3',
        'bx-python'
        ]
    )

#'pbtools/pbtranscript/io/FastaSplitter.py',
#             'pbtools/pbtranscript/ice/IcePartial.py',
#             'pbtools/pbtranscript/ice/IceQuiver.py'
