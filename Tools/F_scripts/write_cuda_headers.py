#!/usr/bin/env python

"""
Search the Fortran source code for subroutines marked as:

  AMREX_DEVICE subroutine sub(a)

    ...

  end subroutine

and maintain a list of these.

Then copy the C++ headers for Fortran files (typically *_F.H) into a
temp directory, modifying any subroutines marked with #pragma gpu to
have both a device and host signature.
"""

from __future__ import print_function

import sys

if sys.version_info < (2, 7):
    sys.exit("ERROR: need python 2.7 or later for dep.py")

if sys.version[0] == "2":
    reload(sys)
    sys.setdefaultencoding('utf8')

import os
import re
import argparse

import find_files_vpath as ffv
import preprocess


TEMPLATE = """
__global__ static void cuda_{}
{{
{}
   int blo[3];
   int bhi[3];
   for (int k = lo[2] + blockIdx.z * blockDim.z + threadIdx.z; k <= hi[2]; k += blockDim.z * gridDim.z) {{
     blo[2] = k;
     bhi[2] = k;
     for (int j = lo[1] + blockIdx.y * blockDim.y + threadIdx.y; j <= hi[1]; j += blockDim.y * gridDim.y) {{
       blo[1] = j;
       bhi[1] = j;
       for (int i = lo[0] + blockIdx.x * blockDim.x + threadIdx.x; i <= hi[0]; i += blockDim.x * gridDim.x) {{
         blo[0] = i;
         bhi[0] = i;
         {};
       }}
     }}
   }}
}}
"""

# for finding just the variable definitions in the function signature (between the ())
decls_re = re.compile("(.*?)(\\()(.*)(\\))", re.IGNORECASE|re.DOTALL)

# for finding a fortran function subroutine marked with AMREX_DEVICE or attributes(device)
fortran_re = re.compile("(AMREX_DEVICE)(\\s+)(subroutine)(\\s+)((?:[a-z][a-z_]+))",
                        re.IGNORECASE|re.DOTALL)
fortran_attributes_re = re.compile("(attributes\\(device\\))(\\s+)(subroutine)(\\s+)((?:[a-z][a-z_]+))",
                                   re.IGNORECASE|re.DOTALL)

# for finding a header entry for a function binding to a fortran subroutine
fortran_binding_re = re.compile("(void)(\\s+)((?:[a-z][a-z_]+))",
                                re.IGNORECASE|re.DOTALL)

class HeaderFile(object):
    """ hold information about one of the headers """

    def __init__(self, filename):

        self.name = filename

        # when we preprocess, the output has a different name
        self.cpp_name = None


def find_targets_from_pragmas(cxx_files, macro_list):
    """read through the C++ files and look for the functions marked with
    #pragma gpu -- these are the routines we intend to offload (the targets),
    so make a list of them"""

    targets = dict()

    for c in cxx_files:
        cxx = "/".join([c[1], c[0]])

        # open the original C++ file
        try:
            hin = open(cxx, "r")
        except IOError:
            sys.exit("Cannot open header {}".format(cxx))

        # look for the appropriate pragma, and once found, capture the
        # function call following it
        line = hin.readline()
        while line:

            # if the line starts with "#pragma gpu", then we need
            # to take action
            if line.startswith("#pragma gpu"):
                # we don't need to reproduce the pragma line in the
                # output, but we need to capture the whole function
                # call that follows
                func_call = ""
                line = hin.readline()
                while not line.strip().endswith(";"):
                    func_call += line
                    line = hin.readline()
                # last line -- remove the semi-colon
                func_call += line.rstrip()[:-1]

                # now split it into the function name and the
                # arguments
                dd = decls_re.search(func_call)
                func_name = dd.group(1).strip().replace(" ", "")

                # Now, for each argument in the function, record
                # if it has a special macro in it.
                targets[func_name] = []
                args = dd.group().strip().split('(', 1)[1].rsplit(')', 1)[0].split(',')
                for j, macro in enumerate(macro_list):
                    targets[func_name].append([])
                    for i, arg in enumerate(args):
                        if macro in arg:
                            targets[func_name][j].append(i)

            line = hin.readline()

    return targets


def convert_headers(outdir, targets, header_files, cpp):
    """rewrite the C++ headers that contain the Fortran routines"""

    print('looking for targets: {}'.format(list(targets)))
    print('looking in header files: {}'.format(header_files))

    # first preprocess all the headers and store them in a temporary
    # location.  The preprocessed headers will only be used for the
    # search for the signature, not as the basis for writing the final
    # CUDA header
    pheaders = []

    for h in header_files:
        hdr = "/".join([h[1], h[0]])
        hf = HeaderFile(hdr)

        # preprocess -- this will create a new file in our temp_dir that
        # was run through cpp and has the name CPP-filename
        cpp.preprocess(hf, add_name="CPP")

        pheaders.append(hf)

    # now scan the preprocessed headers and find any of our function
    # signatures and output to a new unpreprocessed header
    for h in pheaders:

        # open the preprocessed header file -- this is what we'll scan
        try:
            hin = open(h.cpp_name, "r")
        except IOError:
            sys.exit("Cannot open header {}".format(h.cpp_name))

        # we'll keep track of the signatures that we need to mangle
        signatures = {}

        line = hin.readline()
        while line:

            # if the line does not start a function signature that
            # matches one of our targets, then we ignore it.
            # Otherwise, we need to capture the function signature
            found = None

            # strip comments
            idx = line.find("//")
            tline = line[:idx]

            for target in list(targets):
                target_match = fortran_binding_re.search(tline.lower())
                if target_match:
                    if target == target_match.group(3):
                        found = target
                        print('found target {} in header {}'.format(target, h.cpp_name))
                        break

            # we found a target function, so capture the entire
            # signature, which may span multiple lines
            if found is not None:
                launch_sig = ""
                sig_end = False
                while not line.strip().endswith(";"):
                    launch_sig += line
                    line = hin.readline()
                launch_sig += line

                signatures[found] = [launch_sig, targets[found]]

            line = hin.readline()

        hin.close()

        # we've now finished going through the header. Note: there may
        # be more signatures here than we really need, because some may
        # have come in via '#includes' in the preprocessing.


        # Now we'll go back to the original file, parse it, making note
        # of any of the signatures we find, but using the preprocessed
        # version in the final output.

        # open the CUDA header for output
        _, tail = os.path.split(h.name)
        ofile = os.path.join(outdir, tail)
        try:
            hout = open(ofile, "w")
        except IOError:
            sys.exit("Cannot open output file {}".format(ofile))

        # and back to the original file (not preprocessed) for the input
        try:
            hin = open(h.name, "r")
        except IOError:
            sys.exit("Cannot open output file {}".format(ofile))

        signatures_needed = {}

        line = hin.readline()
        while line:

            # if the line does not start a function signature that we
            # need, then we ignore it
            found = None

            # strip comments
            idx = line.find("//")
            tline = line[:idx]

            # if the line is not a function signature that we already
            # captured then we just write it out
            for target in list(signatures):

                target_match = fortran_binding_re.search(tline.lower())
                if target_match:
                    if target == target_match.group(3):
                        found = target
                        signatures_needed[found] = signatures[found]

                        print('found target {} in unprocessed header {}'.format(target, h.name))
                        break

            if found is not None:
                launch_sig = "" + line
                sig_end = False
                while not sig_end:
                    line = hin.readline()
                    launch_sig += line
                    if line.strip().endswith(";"):
                        sig_end = True

            else:
                # this was not one of our device headers
                hout.write(line)

            line = hin.readline()

        # we are done with the pass through the header and we know all
        # of the signatures that need to be CUDAed

        # remove any dupes in the signatures needed
        signatures_needed = list(set(signatures_needed))

        # now do the CUDA signatures
        hout.write("\n")
        hout.write("#include <AMReX_ArrayLim.H>\n")
        hout.write("#include <AMReX_BLFort.H>\n")
        hout.write("#include <AMReX_Device.H>\n")
        hout.write("\n")

        hdrmh = os.path.basename(h.name).strip(".H")

        # Add an include guard -- do we still need this?
        hout.write("#ifndef _cuda_" + hdrmh + "_\n")
        hout.write("#define _cuda_" + hdrmh + "_\n\n")

        # Wrap the device declarations in extern "C"
        hout.write("#ifdef AMREX_USE_CUDA\n")
        hout.write("extern \"C\" {\n\n")

        for name in list(signatures_needed):

            print(signatures[name])

            func_sig = signatures[name][0]

            # First write out the device signature
            device_sig = "__device__ {};\n\n".format(func_sig)

            idx = func_sig.lower().find(name)

            # here's the case-sensitive name
            case_name = func_sig[idx:idx+len(name)]

            # Add _device to the function name.

            device_sig = device_sig.replace(case_name, case_name + "_device")

            # Now write out the global signature. This involves
            # getting rid of the data type definitions and also
            # replacing the lo and hi (which must be in the function
            # definition) with blo and bhi.
            dd = decls_re.search(func_sig)
            vars = []

            has_lo = False
            has_hi = False

            intvect_vars = []

            for n, v in enumerate(dd.group(3).split(",")):

                # we will assume that our function signatures _always_ include
                # the name of the variable
                _tmp = v.split()
                var = _tmp[-1].replace("*", "").replace("&", "").strip()

                # Replace AMReX Fortran macros
                var = var.replace("BL_FORT_FAB_ARG_3D", "BL_FORT_FAB_VAL_3D")
                var = var.replace("BL_FORT_IFAB_ARG_3D", "BL_FORT_FAB_VAL_3D")

                # Get the list of all arguments which contain AMREX_INT_ANYD.
                # Replace them with the necessary machinery, a set of three
                # constant ints which will be passed by value.

                args = signatures[name][0].split('(', 1)[1].rsplit(')', 1)[0].split(',')
                arg_positions = signatures[name][1][0]

                if arg_positions != []:
                    print("arg_positions", arg_positions)
                    print("args = ", args)
                    for arg_position in arg_positions:
                        if n == arg_position:
                            arg = args[arg_position]
                            var = arg.split()[-1]
                            print("arg, var = ", arg, var)
                            func_sig = func_sig.replace(arg, "const int {}_1, const int {}_2, const int {}_3".format(var, var, var))
                            print("func_sig = ", func_sig)
                            device_sig = device_sig.replace(arg, "const int* {}".format(var))
                            intvect_vars.append(var)
                else:
                    # Handle the legacy case where we just passed in lo, hi
                    if var == "lo":
                        func_sig = func_sig.replace("const int* lo", "const int {}_1, const int {}_2, const int {}_3".format(var, var, var))
                        intvect_vars.append(var)
                    elif var == "hi":
                        func_sig = func_sig.replace("const int* hi", "const int {}_1, const int {}_2, const int {}_3".format(var, var, var))
                        intvect_vars.append(var)

                if var == "lo":
                    var = "blo"
                    has_lo = True

                elif var == "hi":
                    var = "bhi"
                    has_hi = True

                vars.append(var)

            if not has_lo or not has_hi:
                sys.exit("ERROR: function signature must have variables lo and hi defined:\n--- function name:\n {} \n--- function signature:\n {}\n---".format(name, func_sig))

            # reassemble the function sig
            all_vars = ", ".join(vars)
            new_call = "{}({})".format(case_name + "_device", all_vars)

            # Collate all the IntVects that we are going to make
            # local copies of.

            intvects = ""

            if len(intvect_vars) > 0:
                for intvect in intvect_vars:
                    intvects += "   int {}[3] = {{{}_1, {}_2, {}_3}};\n".format(intvect, intvect, intvect, intvect)


            hout.write(device_sig)
            hout.write(TEMPLATE.format(func_sig[idx:].replace(';',''), intvects, new_call))
            hout.write("\n")


        # Close out the extern "C" region
        hout.write("\n}\n")
        hout.write("#endif\n")

        # Close out the include guard
        hout.write("\n")
        hout.write("#endif\n")

        hin.close()
        hout.close()


def convert_cxx(outdir, cxx_files):
    """look through the C++ files for "#pragma gpu" and switch it
    to the appropriate CUDA launch macro"""

    print('looking in C++ files: {}'.format(cxx_files))

    for c in cxx_files:
        cxx = "/".join([c[1], c[0]])

        # open the original C++ file
        try:
            hin = open(cxx, "r")
        except IOError:
            sys.exit("Cannot open header {}".format(cxx))

        # open the C++ file for output
        _, tail = os.path.split(cxx)
        ofile = os.path.join(outdir, tail)
        try:
            hout = open(ofile, "w")
        except IOError:
            sys.exit("Cannot open output file {}".format(ofile))

        # look for the appropriate pragma, and once found, capture the
        # function call following it
        line = hin.readline()
        while line:

            # if the line starts with "#pragma gpu", then we need
            # to take action
            if line.startswith("#pragma gpu"):
                # we don't need to reproduce the pragma line in the
                # output, but we need to capture the whole function
                # call that follows
                func_call = ""
                line = hin.readline()
                while not line.strip().endswith(";"):
                    func_call += line
                    line = hin.readline()
                # last line -- remove the semi-colon
                func_call += line.rstrip()[:-1]

                # now split it into the function name and the
                # arguments
                print(func_call)
                dd = decls_re.search(func_call)
                func_name = dd.group(1).strip().replace(" ", "")
                args = dd.group(3)

                # finally output the code in the form we want, with
                # the device launch
                hout.write("dim3 {}numBlocks, {}numThreads;\n" \
                            "Device::grid_stride_threads_and_blocks({}numBlocks, {}numThreads);\n" \
                            "#if ((__CUDACC_VER_MAJOR__ > 9) || (__CUDACC_VER_MAJOR__ == 9 && __CUDACC_VER_MINOR__ >= 1))\n" \
                            "CudaAPICheck(cudaFuncSetAttribute(&cuda_{}, cudaFuncAttributePreferredSharedMemoryCarveout, 0));\n" \
                            "#endif\n" \
                            "cuda_{}<<<{}numBlocks, {}numThreads, 0, Device::cudaStream()>>>\n    ({});\n".format(
                                func_name, func_name, func_name, func_name, func_name, func_name, func_name, func_name, args))

            else:
                # we didn't find a pragma
                hout.write(line)

            line = hin.readline()


if __name__ == "__main__":

    parser = argparse.ArgumentParser()

    parser.add_argument("--vpath",
                        help="the VPATH to search for files")
    parser.add_argument("--fortran",
                        help="the names of the fortran files to search")
    parser.add_argument("--headers",
                        help="the names of the header files to convert")
    parser.add_argument("--cxx",
                        help="the names of the C++ files to process pragmas")
    parser.add_argument("--output_dir",
                        help="where to write the new header files",
                        default="")
    parser.add_argument("--cpp",
                        help="command to run C preprocessor on .F90 files first.  If omitted, then no preprocessing is done",
                        default="")
    parser.add_argument("--defines",
                        help="defines to send to preprocess the files",
                        default="")
    parser.add_argument("--exclude_defines",
                        help="space separated string of directives to remove from defines",
                        default="")


    args = parser.parse_args()

    defines = args.defines

    if args.exclude_defines != "":
        excludes = args.exclude_defines.split()
        for ex in excludes:
            defines = defines.replace(ex, "")

    print("defines: ", defines)

    if args.cpp != "":
        cpp_pass = preprocess.Preprocessor(temp_dir=args.output_dir, cpp_cmd=args.cpp,
                                           defines=defines)
    else:
        cpp_pass = None

    headers, _ = ffv.find_files(args.vpath, args.headers)
    cxx, _ = ffv.find_files(args.vpath, args.cxx)


    # part I: we need to find the names of the Fortran routines that
    # are called from C++ so we can modify the header in the
    # corresponding *_F.H file.

    # A list of specific macros that we want to look for in each target.

    macro_list = ['AMREX_INT_ANYD']

    # look through the C++ for routines launched with #pragma gpu
    targets = find_targets_from_pragmas(cxx, macro_list)

    # copy the headers to the output directory, replacing the
    # signatures of the target Fortran routines with the CUDA pair
    convert_headers(args.output_dir, targets, headers, cpp_pass)


    # part II: for each C++ file, we need to expand the `#pragma gpu`

    convert_cxx(args.output_dir, cxx)
