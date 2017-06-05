"""
File contaning methosd and functions for analyzing TCGA data
"""
import os 
import sys
import numpy
import glob
import pickle
import sortedcontainers
import intervaltree
import collections
import numpy as np

import tcga_parser
import tcga_processor
import tcga_imaging
import gtf_parser

RESULTS_DIR = os.path.join(
    tcga_parser.DRIVE_ROOT,
    "results"
)

COMMON_GENES_FILE = os.path.join(
    RESULTS_DIR,
    "common_genes.txt"
)

def most_different_genes():
    """
    Calculates the most idfferentially expressed intervals from cnv data
    """
    SIZE_CUTOFF = 10
    # Load in all the data from each patient.
    tcga_object_files = glob.glob(os.path.join(tcga_parser.DATA_ROOT, "tcga_patient_objects", "*.pickled"))
    assert len(tcga_object_files) > 0
    tcga_patients = []
    for object_file in tcga_object_files:
        with open(object_file, 'rb') as handle:
            tcga_patients.append(pickle.load(handle))
    assert len(tcga_patients) > 0
    print("Loaded in %i patients' data" % len(tcga_patients))

    # Do the analysis for protein expresion data
    protein_expression_data = collections.defaultdict(list)
    for patient in tcga_patients:
        for gene, entry in patient.prot_exp.items():
            assert len(entry.keys()) == 2
            exp_key = list(entry.keys())[0] if list(entry.keys())[0] != "Sample REF" else list(entry.keys())[1]
            try:
                protein_expression_data[gene].append(float(entry[exp_key]))
            except ValueError:
                if entry[exp_key] == "Protein Expression":
                    continue
                else:
                    raise ValueError("Cannot parse the following value as float: %s" % entry[exp_key])
    protein_expression_stddev = {}
    for gene, values in protein_expression_data.items():
        if len(values) < SIZE_CUTOFF:
            continue # we do not want to process entreis that have fewer than SIZE_CUTOFF observations
        protein_expression_stddev[gene] = np.std(values)
    # The beginning of the list is lowest std dev and the end is the highest stddev
    genes_sorted_by_prot_exp = sorted(protein_expression_stddev.keys(), key=lambda x: protein_expression_stddev[x])
    # for gene in genes_sorted_by_prot_exp:
    #     print("%s\t%f" % (gene, protein_expression_stddev[gene]))
    
    # Do the analysis for rna seq data
    rna_seq_data = collections.defaultdict(list)
    for patient in tcga_patients:
        for gene, entry in patient.gene_exp.items():
            rna_seq_data[gene].append(float(entry))
    rna_expression_stddev = {}
    for gene, values in rna_seq_data.items():
        if len(values) < SIZE_CUTOFF:
            continue
        rna_expression_stddev[gene] = np.std(values)
    genes_sorted_by_rna_exp = sorted(rna_expression_stddev.keys(), key=lambda x: rna_expression_stddev[x])
    # Print for debugging
    for gene in genes_sorted_by_rna_exp:
        print("%s\t%f" % (gene, rna_expression_stddev[gene]))
    
    # Do the analysis for CNV data
    cnv_data = collections.defaultdict(intervaltree.IntervalTree)
    for patient in tcga_patients:
        for item in patient.cnv:
            print(item)

def get_all_genomic_breakpoints(datatypes=['rna', 'cnv']):
    """
    Gets all the genomic break points for each chromosome. These breakpoints are are union of the
    breakpoints in all the datatypes given in the argment (possibilities: rna, cnv, protein). These
    breakpoints are used to split each chromosome into discrete bins, such that each bin
    """
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

    # Load in all the patients
    patients = []
    for patient_file in tcga_patient_files:
        with open(patient_file, 'rb') as handle:
            patient = pickle.load(handle)
        assert isinstance(patient, tcga_parser.TcgaPatient)
        patients.append(patient)

    # Figure out the breakpoints that we need to have every single thing line up
    chromosome_breakpoints = collections.defaultdict(sortedcontainers.SortedSet)
    for patient in patients:
        try:
            # This is List<Dictionary<Chromosome, IntervalTree>>
            chromosome_breakpoints_per_patient = []
            if 'rna' in datatypes:
                rna_intervals = tcga_imaging.gene_to_interval(patient.gene_values(), ensembl_genes)
                rna_intervals_sorted = {chromosome:tcga_imaging.interval_to_sorteddict(itree) for chromosome, itree in rna_intervals.items()}
                chromosome_breakpoints_per_patient.append(rna_intervals_sorted)
            if 'protein' in datatypes:
                protein_intervals = tcga_imaging.gene_to_interval(patient.prot_values(), ensembl_genes)
                protein_intervals_sorted = {chromosome:tcga_imaging.interval_to_sorteddict(itree) for chromosome, itree in protein_intervals.items()}
                chromosome_breakpoints_per_patient.append(protein_intervals_sorted)
            if 'cnv' in datatypes:
                cnv_intervals = patient.cnv_values()
                cnv_intervals_sorted = {chromosome:tcga_imaging.interval_to_sorteddict(itree) for chromosome, itree in cnv_intervals.items()}
                chromosome_breakpoints_per_patient.append(cnv_intervals_sorted)

            for sorted_interval_dict in chromosome_breakpoints_per_patient:
                for chromosome, points in sorted_interval_dict.items():
                    for coord_set in points.keys():
                        chromosome_breakpoints[chromosome].add(coord_set[0])
                        chromosome_breakpoints[chromosome].add(coord_set[1])
        except AttributeError:
            print("%s was skipped because of attribute error" % patient.barcode)
            continue
    
    # Total the breakpoints and write them to an output file
    if not os.path.isdir(RESULTS_DIR): # Make the results directory if it doesn't already exist
        os.makedirs(RESULTS_DIR)
    breakpoints_output_file = os.path.join(RESULTS_DIR, "breakpoints.txt")
    total_breakpoint_count = 0
    with open(breakpoints_output_file, 'w') as handle:
        for chromosome, breakpoints in chromosome_breakpoints.items():
            # Print to terminal
            print("%s\t%i breakpoints" % (chromosome, len(breakpoints)))
            total_breakpoint_count += len(breakpoints)
            # Write to file
            handle.write(chromosome + ": " + ','.join([str(x) for x in breakpoints]) + "\n")
        print("Total: %i" % total_breakpoint_count)
    print("Wrote breakpoint data to: %s" % breakpoints_output_file)


def get_genes_intersection_cnv_rna():
    """
    Get the genes that are common between CNV datasets and RNA datasets
    """
    patients = tcga_processor.load_tcga_objects()
    ensembl_genes = gtf_parser.Gtf(os.path.join(tcga_parser.DRIVE_ROOT, "Homo_sapiens.GRCh37.87.gtf"),
                                   "gene", set(['ensembl_havana']))
    cnv_breakpoints = collections.defaultdict(sortedcontainers.SortedSet)
    rna_breakpoints = collections.defaultdict(sortedcontainers.SortedSet)

    usable_gene_sets = []
    for patient in patients:
        try:
            usable_genes = set()
            cnv_intervals = patient.cnv_values()
            cnv_intervals_sorted = {chromosome: tcga_imaging.interval_to_sorteddict(itree) for chromosome, itree in cnv_intervals.items()}
            rna_expression = patient.gene_values()
            for gene, expression_value in rna_expression.items():
                # Get the coresponding intervals for that gene
                # print(gene)
                ensembl_entries = ensembl_genes.get_gene_entries(gene)
                if not ensembl_entries:
                    continue
                ensembl_entry = sorted(ensembl_entries, key=lambda x: int(x['gene_version']))[-1]
                start, stop = ensembl_entry['start'], ensembl_entry['stop']
                # Find the NV intervals that overlap
                overlapping_cnv_queries = cnv_intervals[ensembl_entry['chromosome']][start:stop]
                if not overlapping_cnv_queries:
                    continue
                usable_genes.add(gene)
            usable_gene_sets.append(usable_genes)
        except AttributeError:
            print("{0} Skipped due to attribute error".format(patient.barcode))
    # Figure out the genes that are common to all
    fully_common_genes = set()
    for gene in usable_gene_sets[0]:
        if all([gene in x for x in usable_gene_sets]):
            fully_common_genes.add(gene)
    # There are 15368 genes that are common to all
    # Write them to a file so we can access them later on
    print(len(fully_common_genes))
    with open(COMMON_GENES_FILE, 'w') as handle:
        for gene in fully_common_genes:
            handle.write(gene + "\n")

def get_range_of_values():
    """
    Gets the range of values (i.e. min and maximum value) within each datatype
    """
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

    # Load in all the patients
    patients = []
    for patient_file in tcga_patient_files:
        with open(patient_file, 'rb') as handle:
            patient = pickle.load(handle)
        assert isinstance(patient, tcga_parser.TcgaPatient)
        patients.append(patient)

    cnv_values = []
    rna_values = []
    prot_values = []
    for patient in patients:
        if patient.cnv_values() is not None:
            cnv_values.extend([x.data for chromosome, itree in patient.cnv_values().items() for x in itree])
        if patient.gene_values() is not None:
            rna_values.extend([x for x in patient.gene_values().values()])
        if patient.prot_values() is not None:
            prot_values.extend([x for x in patient.prot_values().values()])
    
    # Define the file that we will write to
    if not os.path.isdir(RESULTS_DIR):
        os.makedirs(RESULTS_DIR)
    ranges_file = os.path.join(RESULTS_DIR, "ranges.txt")
    with open(ranges_file, 'w') as handle:
        handle.write("cnv %f - %f\n" % (min(cnv_values), max(cnv_values)))
        handle.write("gene %f - %f\n" % (min(rna_values), max(rna_values)))
        handle.write("prot %f - %f\n" % (min(prot_values), max(prot_values)))
    print("Wrote minimum/maximum values for each datatype to: %s" % ranges_file)


def main():
    # get_all_genomic_breakpoints()
    get_genes_intersection_cnv_rna()
    # get_range_of_values()
    # pass

if __name__ == "__main__":
    main()
