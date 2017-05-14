"""
Functions and classes for creating images that are expressive of
per-patient expression profiles

Notes to self:
   * Convert gene names into the intervaltree, much like how the cnvs
   are represented. This allows us to preserve the level of detail
   granted by CNV data, and combine it with the more general information
   given by rna/protein expression data
   * Another idea is to use every single data point that we have observations
   for, i.e a union of the cnv, rna, and protein expression data. we'd then
   feed these images into the neural net and let that decide for itself what is
   important. If we do this, we need to make sure that we are ordering pixels
   by their genomic coordinates such that areas that are close to each other
   end up getting grouped together, simulating the idea of an amplicon
"""

import os
import glob
import pickle
import intervaltree
import sortedcontainers
import collections
import gtf_parser
import tcga_parser
from PIL import Image
import numpy as np
import scipy.misc
import time
import math
import functools
import multiprocessing

import tcga_analysis

IMAGES_DIR = os.path.join(
    tcga_parser.DRIVE_ROOT,
    "results",
    "images"
)

##### HERE ARE ALL THE IMAGE CREATORS #####

def value_within_range(value, minimum, maximum):
    """
    Checks whether the given value falls wtihin the given minimum/maximum range, inclusive
    """
    if math.isclose(value, minimum, rel_tol=1e-5) or math.isclose(value, maximum, rel_tol=1e-5):
        return True
    if value >= minimum and value <= maximum:
        return True
    return False

def create_image_full_gene_intersection(patient):
    """
    Consumes a tcga patient and creates an image that is a composite of
    only genes/positions that are completely within
    """
    assert isinstance(patient, tcga_parser.TcgaPatient)

    # Only retain genes that are common to both genes and protein expression
    # data
    common_genes = set(patient.gene_exp.keys()).intersection(set(patient.prot_exp.keys()))
    print(len(common_genes)) # This is only 44...we may want to try something else


def create_image_full_union(patient, gene_intervals, breakpoints_file, ranges_file):
    """
    Another idea is to use every single data point that we have observations
    for, i.e a union of the cnv, rna, and protein expression data. we'd then
    feed these images into the neural net and let that decide for itself what is
    important. If we do this, we need to make sure that we are ordering pixels
    by their genomic coordinates such that areas that are close to each other
    end up getting grouped together, simulating the idea of an amplicon
    """
    assert isinstance(patient, tcga_parser.TcgaPatient)
    assert isinstance(gene_intervals, gtf_parser.Gtf)

    start_time = time.time()
    # reads in the data as both interval trees and in sorted dicts
    try:
        rna_intervals = gene_to_interval(patient.gene_values(), gene_intervals)
        rna_intervals_sorted = {chromosome:interval_to_sorteddict(itree) for chromosome, itree in rna_intervals.items()}
        protein_intervals = gene_to_interval(patient.prot_values(), gene_intervals)
        protein_intervals_sorted = {chromosome:interval_to_sorteddict(itree) for chromosome, itree in protein_intervals.items()}
        cnv_intervals = patient.cnv_values()
        cnv_intervals_sorted = {chromosome:interval_to_sorteddict(itree) for chromosome, itree in cnv_intervals.items()}
    except AttributeError:
        # Some data may be missing for some patients - for those we simply dont' generate an image
        return None

    # # Read in the ranges and breakpoitns file
    breakpoints = {}
    with open(breakpoints_file, 'r') as handle:
        for line in handle:
            line = line.rstrip()
            chromosome, points = line.split(": ")
            breakpoints[chromosome] = sortedcontainers.SortedList([int(x) for x in points.split(",")])
    ranges = {}
    with open(ranges_file, 'r') as handle:
        for line in handle:
            line = line.rstrip()
            datatype, minimum, _null, maximum = line.split()
            ranges[datatype] = (float(minimum), float(maximum))

    # In total we have 328934 breakpoints, which comes out to just under 600 * 600
    # https://stackoverflow.com/questions/12062920/how-do-i-create-an-image-in-pil-using-a-list-of-rgb-tuples
    # Create the template for the image that we are going to create
    width = max([len(x) for x in breakpoints.values()])
    height = len(breakpoints.keys())
    channels = 3 # RGB
    img = np.zeros((height, width, channels), dtype=np.uint8) # unsigned 8-bit integers are 0-255
    # We walk through the chromosomes
    for channel_index, sorted_intervals in enumerate([cnv_intervals_sorted, rna_intervals_sorted, protein_intervals_sorted]):
        for row_index, chromosome in enumerate(breakpoints.keys()):
            # Fill in CNV data on a scale of 0-255
            try:
                values_for_chromosome = sorted_intervals[chromosome]
            except KeyError:
                # This chromosome doesn't exist for this datatype - oh well move on
                continue
            for start_stop_tuple, value in values_for_chromosome.items():
                # Normalize the value to be within the 0-255 range
                if channel_index == 0:
                    minimum, maximum = ranges['cnv']
                elif channel_index == 1:
                    minimum, maximum = ranges['gene']
                elif channel_index == 2:
                    minimum, maximum = ranges['prot']
                else:
                    raise ValueError("Unrecognized channel index: %i" % channel_index)
                if not value_within_range(value, minimum, maximum):
                    raise ValueError("%s WARNING: Given value %f does not fall in the min/max range: %f/%f" % (patient.barcode, value, minimum, maximum))
                value_normalized = np.uint8((float(value) - float(minimum)) / float(maximum) * 255)
                
                start, stop = start_stop_tuple # Figure out the positions where it starts/stops
                start_index = breakpoints[chromosome].index(start) # Figure out where those coordinates lie on the list of sorted breakpoints
                stop_index = breakpoints[chromosome].index(stop)
                if not stop_index > start_index:
                    print("%s WARNING: Start index (%i) is not less than stop index (%i) for channel %i, chromosome %s" % (patient.barcode, start, stop, channel_index, chromosome))
                for col_index in range(start_index, stop_index + 1): # +1 to be inclusive of the stop index
                    img[row_index][col_index][channel_index] = value_normalized

    if not os.path.isdir(IMAGES_DIR):
        os.makedirs(IMAGES_DIR)
    image_path = os.path.join(IMAGES_DIR, "%s.expression.png" % patient.barcode)
    scipy.misc.imsave(image_path, img)
    print("Generated %s in %f seconds" % (image_path, time.time() - start_time))

##### HERE ARE THE UTILITY FUNCTIONS FOR THEM #####


def gene_to_interval(gene_dict, gtf, verbose=False):
    """
    Converts a dictionary that maps genes to chromosomes to entries to a
    intervaltree of entries. Things that have no corresponding intervals
    associated in the given gtf are left out. The gtf should be read in for
    genes, since that is what we are querying (e.g. not exons)
    """
    assert isinstance(gtf, gtf_parser.Gtf)
    itree = collections.defaultdict(intervaltree.IntervalTree)

    counter = 0
    for key, value in gene_dict.items():
        gtf_entries = gtf.get_gene_entries(key)
        if len(gtf_entries) == 0:
            continue
        if len(gtf_entries) > 1:
            # There was more than one. pick the one with the highest gene version
            entry_versions = [int(x['gene_version']) for x in gtf_entries]
            highest_version = max(entry_versions)
            accept_indicies = [x for x, y in enumerate(entry_versions) if y == highest_version]
            if len(accept_indicies) > 1:
                continue
            elif len(accept_indicies) == 0:
                raise RuntimeError("Could not identify a highest version")
            assert len(accept_indicies) == 1
            gtf_entry = gtf_entries[accept_indicies[0]]
        else:
            gtf_entry = gtf_entries[0]

        itree[gtf_entry['chromosome']][gtf_entry['start']:gtf_entry['stop']] = value
        counter += 1
    if verbose:
        print("Converted %i/%i entries from gene dict to intervaltree" % (counter, len(gene_dict)))
    return itree

def interval_to_sorteddict(interval_tree):
    """
    Converts an interval tree to a sorted dictionary where the keys of the dictionary
    are tuples made up of each interval's start and stop positions. This allows us to
    traverse the data in the interval tree in sorted order, instead of the arbitrary
    order that the interval tree returns in its iterator
    """
    assert isinstance(interval_tree, intervaltree.IntervalTree)
    converted = sortedcontainers.SortedDict()
    for interval in interval_tree:
        converted[(interval.begin, interval.end)] = interval.data
    return converted


def main():
    """Runs the script"""
    # Load in a dummy file
    pattern = os.path.join(
        tcga_parser.DATA_ROOT,
        "tcga_patient_objects",
        "TCGA*.pickled"
    )
    tcga_patient_files = glob.glob(pattern)
    ensembl_genes = gtf_parser.Gtf(os.path.join(tcga_parser.DRIVE_ROOT, "Homo_sapiens.GRCh37.87.gtf"),
                                   "gene", set(['ensembl_havana']))
    if len(tcga_patient_files) == 0:
        raise RuntimeError("Found no files matching pattern:\n%s" % pattern)

    breakpoints_file = os.path.join(tcga_analysis.RESULTS_DIR, "breakpoints.txt")
    ranges_file = os.path.join(tcga_analysis.RESULTS_DIR, "ranges.txt")
    patients = []
    for patient_file in tcga_patient_files:
        with open(patient_file, 'rb') as handle:
            patient = pickle.load(handle)
        assert isinstance(patient, tcga_parser.TcgaPatient)
        patients.append(patient)
    #     create_image_full_union(
    #         patient,
    #         ensembl_genes,
    #         os.path.join(tcga_analysis.RESULTS_DIR, "breakpoints.txt"),
    #         os.path.join(tcga_analysis.RESULTS_DIR, "ranges.txt")
    #     )
    # Create the images in parallel
    pool = multiprocessing.Pool(4)
    pool.map(functools.partial(create_image_full_union, gene_intervals=ensembl_genes, breakpoints_file=breakpoints_file, ranges_file=ranges_file), patients)

if __name__ == "__main__":
    main()
