#!/usr/bin/env python
from phylobarcode.pb_common import *  ## better to have it in json? imports itertools, pathlib
import pandas as pd, numpy as np
import io, multiprocessing, shutil, gffutils
from Bio.Blast import NCBIXML
from Bio import Seq, SeqIO
from Bio.SeqRecord import SeqRecord

# legacy code, no need to create a separate logger
#log_format = logging.Formatter(fmt='phylobarcode_fasgff %(asctime)s [%(levelname)s] %(message)s', datefmt="%Y-%m-%d %H:%M")
logger = logging.getLogger("phylobarcode_global_logger")

#logger.propagate = False
#stream_log = logging.StreamHandler() 
#stream_log.setFormatter(log_format)
#stream_log.setLevel(logging.INFO)
#logger.addHandler(stream_log)

# TODO:
# 1. if plasmid has riboprot genes, we exclude them from merged fasta+GFF but not from coordinates file (e.g. plasmid
#    NZ_CP007068.1 belonging to Rhizobium leguminosarum bv. trifolii CB782 - GCF_000520875.1.fna.gz)
#    -> currently we are fine since plasmids do not have several riboprot genes 
# 2. deduplicate (sourmash or genus information)

def merge_fasta_gff (fastadir=None, gffdir=None, fasta_tsvfile = None, gff_tsvfile = None, gtdb = None, scratch=None, output=None, nthreads = 1):
    hash_name = '%012x' % random.randrange(16**12)  # use same file random file name for all files (notice that main script should have taken care of these)
    if fastadir is None: 
        logger.error("No fasta directory provided"); return
    if not os.path.isdir (fastadir):
        logger.error(f"Fasta directory provided {fastadir} does not exist or is not a proper directory"); return
    if gffdir is None: 
        logger.error("No GFF3 directory provided"); return
    if not os.path.isdir (gffdir):
        logger.error(f"GFF3 directory provided {gffdir} does not exist or is not a proper directory"); return
    if output is None: ## this should not happen if function called from main script
        prefix = f"fastagff.{hash_name}"
        logger.warning (f"No output file specified, using {prefix} as prefix")
    if scratch is None: ## this should not happen if function called from main script; use current directory 
        scratch = f"scratch.{hash_name}"
    # create scratch directory (usually it's a subdirectory of the user-given scratch directory)
    pathlib.Path(scratch).mkdir(parents=True, exist_ok=True) # python 3.5+ create dir if it doesn't exist

    # check if directories contain fasta files first
    fasta_files = list_of_files_by_extension (fastadir, ['fasta', 'fa', 'fna', 'faa', 'ffn', 'faa', 'fas'])
    logger.info(f"Found {len(fasta_files)} fasta files in {fastadir}")
    if (len(fasta_files) < 1):
        logger.error(f"Not enough fasta files found in {fastadir}")
        return

    # get sequence names as dataframe
    fasta_files, fasta_tsv = update_tsv_from_filenames (fasta_files, fasta_tsvfile, "fasta_file")
    if len(fasta_files):
        logger.info(f"{len(fasta_files)} fasta files in {fastadir} not described in {fasta_tsvfile}")
        if (nthreads > 1):
            logger.info(f"Using up to {nthreads} threads to read fasta headers")
            chunks = generate_thread_chunks (fasta_files, nthreads) # list of filename lists, one per thread
            from multiprocessing import Pool
            from functools import partial
            with Pool(len(chunks)) as p:
                results = p.map(partial(split_headers_in_fasta), chunks)
            a = [row for sublist in results if sublist is not None for row in sublist] # [[[1,2]], [[7,8],[10,11]]] -> [[1,2],[7,8],[10,11]]
        else:
            logger.info(f"Using a single thread to read fasta headers")
            a = split_headers_in_fasta (fasta_files) # list of lists (samples=rows, features=columns)

        a = list(map(list, zip(*a))) # transpose list of lists (https://stackoverflow.com/questions/6473679/python-transpose-list-of-lists)
        a = {"fasta_file": a[0], "seqid": a[1], "fasta_description": a[2]} # dictionary of lists (one row is chromosome and others are plasmids usually)
        df_fasta = pd.DataFrame.from_dict(a, orient='columns')
        if fasta_tsv is not None:
            logger.info(f"FASTA: {len(fasta_tsv)} sequences already in tsv file {fasta_tsvfile}")
            logger.info(f"FASTA: {len(df_fasta)} new sequences found in directory {fastadir}")
            df_fasta = pd.concat([df_fasta, fasta_tsv], ignore_index=True)
        else:
            logger.info(f"FASTA: found {len(df_fasta)} sequences in directory {fastadir}")
        tsvfilename = f"{output}_fasta.tsv.xz"
        df_fasta.to_csv (tsvfilename, sep="\t", index=False)
        logger.info(f"All fasta entries wrote to {tsvfilename}")
    else:
        logger.info (f"All fasta files already found in tsv file {fasta_tsvfile}, with {len(fasta_tsv)} entries")
        df_fasta = fasta_tsv

    # check if directories contain gff files first
    gff_files = list_of_files_by_extension (gffdir, ['gff', 'gff3'])
    logger.info(f"Found {len(gff_files)} gff files in {gffdir}")
    if (len(gff_files) < 1):
        logger.error(f"Not enough gff files found in {gffdir}")
        return

    # get GFF chromosomes (excludes plasmids), using scratch dir to store the sqlite db, as dataframe
    gff_files, gff_tsv = update_tsv_from_filenames (gff_files, gff_tsvfile, "gff_file")
    if len(gff_files):
        logger.info(f"{len(gff_files)} gff files in {gffdir} not described in {gff_tsvfile}")
        if (nthreads > 1):
            logger.info(f"Using up to {nthreads} threads to read gff headers")
            chunks = generate_thread_chunks (gff_files, nthreads)
            from multiprocessing import Pool
            from functools import partial
            with Pool(len(chunks)) as p:
                results = p.map(partial(split_region_elements_in_gff, scratchdir=scratch), chunks)
            a = [row for sublist in results if sublist is not None for row in sublist] # [[[1,2]], [[7,8],[10,11]]] -> [[1,2],[7,8],[10,11]]
        else:
            logger.info(f"Using a single thread to read gff headers")
            a = split_region_elements_in_gff (gff_files, scratchdir=scratch) # list of lists (samples=rows, features=columns)

        a = list(map(list, zip(*a))) # transpose list of lists so that each row is one feature
        a = {"gff_file": a[0], "seqid": a[1], "gff_description": a[2], "gff_taxonid": a[3]} # dictionary of lists (usually one row only since chromosome)
        df_gff = pd.DataFrame.from_dict(a, orient='columns')
        if gff_tsv is not None:
            logger.info(f"GFF: {len(gff_tsv)} sequences already in tsv file {gff_tsvfile}")
            logger.info(f"GFF: {len(df_gff)} new sequences found in directory {gffdir}")
            df_gff = pd.concat([df_gff, gff_tsv], ignore_index=True)
        else:
            logger.info(f"GFF: found {len(df_gff)} sequences in directory {gffdir}")
        tsvfilename = f"{output}_gff.tsv.xz"
        df_gff.to_csv (tsvfilename, sep="\t", index=False)
        logger.info(f"All GFF entries wrote to {tsvfilename}")
    else:
        logger.info (f"All GFF files already found in tsv file {gff_tsvfile}, with {len(gff_tsv)} entries")
        df_gff = gff_tsv

    # delete scratch subdirectory and all its contents
    shutil.rmtree(pathlib.Path(scratch)) # delete scratch subdirectory

    # merge dataframes using seqid as key, keeping only rows found in both

    if (gtdb is None):
        logger.warning("No GTDB taxonomy file provided, the table with matching genomes will _not_ be created. Use the "
                "generated tsv files together with the GTDB file in order to produce the final table.")
        return

    df = pd.merge(df_fasta, df_gff, on='seqid', how='inner')
    if (len(df) < 1):
        logger.error(f"No common entries found in fasta and gff files. Cannot produce the final table with merged info.")
        return
    logger.info(f"Found {len(df)} common entries in fasta and gff files; Will now read GTDB file {gtdb} and merge")

    # get GTDB taxonomy as dataframe and merge with existing dataframe (gff+fasta info)
    df = read_gtdb_taxonomy_and_merge (gtdb, df)
    full_dlen = len(df) - df["gtdb_accession"].isnull().sum() # sum=count null values

    tsvfilename = f"{output}_merged.tsv.xz"
    logger.info(f"Found {full_dlen} samples with complete information; writing all (including incomplete) to {tsvfilename}")
    print (df.head())
    df.to_csv (tsvfilename, sep="\t", index=False)

def list_of_files_by_extension (dirname, extension):
    files = []
    for ext in extension:
        files += glob.glob(f"{dirname}/*.{ext}") + glob.glob(f"{dirname}/*.{ext}.*")
    return files

def update_tsv_from_filenames (files, tsvfile, columnname):
    if tsvfile is None:
        return files, None
    if not os.path.isfile (tsvfile):
        logger.error(f"tsv file {tsvfile} does not exist or is not a proper file"); return files, None
    df = pd.read_csv (tsvfile, sep="\t", dtype=str)
    if columnname not in df.columns:
        logger.error(f"tsv file {tsvfile} does not have column {columnname}"); return files, None
    new_files = df[columnname].unique()
    new_files = [f for f in files if os.path.basename(f) not in new_files]
    old_files = list(set(files) - set(new_files))
    old_files = [os.path.basename(f) for f in old_files]
    df = df[df[columnname].isin(old_files)] # keep only rows with filenames found in files
    return new_files, df

def split_headers_in_fasta (fasta_file_list):
    a2 = []
    for fas in fasta_file_list:
        # example header: >NZ_CP010000.1 Escherichia coli str. K-12 substr. MG1655, complete genome
        # thus we remove everything after first comma ("complete genome" in most cases)
        a = [x.split(",")[0] for x in read_fasta_headers_as_list (fas)] # read_fasta_headers is defined in pb_common.py
        a = [[os.path.basename(fas), x.split(" ",1)[0], x.split(" ",1)[1]] for x in a] # filename +  split on first space
        a2.extend(a)
    return a2

def split_region_elements_in_gff (gff_file_list, scratchdir):
    database_file = os.path.join(scratchdir, os.path.basename(gff_file_list[0]) + ".db")
    a = []
    for gff_file in gff_file_list:
        db = gffutils.create_db (gff_file, database_file, merge_strategy='create_unique', keep_order=True, force=True) # force to overwrite existing db
        for ft in db.features_of_type('region', order_by='start'):
            if "genome" in ft.attributes and ft.attributes['genome'][0] == 'chromosome': # skip plasmids
                longname = ""
                if ("old-name" in ft.attributes): 
                    longname = ft.attributes["old-name"][0] + ";" ## alternative to ft["old-name"]
                if ("type-material" in ft.attributes): 
                    longname = ft.attributes["type-material"][0] + ";" 
                if ("strain" in ft.attributes):
                    longname = ft.attributes["strain"][0] + ";"
                a.append ([os.path.basename(gff_file), ft.seqid, longname, ft["Dbxref"][0].replace("taxon:","")]) # filename + seqid + longname + Dbxref
    return a

def read_gtdb_taxonomy_and_merge (gtdb_file, df):
    gtdb_columns_keep = ['accession', 'gtdb_genome_representative', 'gtdb_taxonomy', 'ssu_query_id', 'ncbi_assembly_name', 
            'ncbi_genbank_assembly_accession', 'ncbi_strain_identifiers', 'ncbi_taxid', 'ncbi_taxonomy'] #  'ncbi_taxonomy_unfiltered' is not used

    df_gtdb = pd.read_csv(gtdb_file, sep="\t", dtype=str)
    df_gtdb = df_gtdb[gtdb_columns_keep] # remove most columns

    gtdb_columns_rename = {'accession': 'gtdb_accession', 'ssu_query_id': 'seqid'}
    df_gtdb.rename(columns=gtdb_columns_rename, inplace=True) # rename columns to match other tables
    map_df = df[["seqid", "fasta_file"]].drop_duplicates() # map seqid to fasta s.t. all seqids from fasta receive GTDB info 
    df_gtdb = pd.merge(df_gtdb, map_df, on='seqid', how='inner').drop(columns=['seqid']) # fasta_file column will be key
    df_gtdb = pd.merge(df, df_gtdb, on='fasta_file', how='left') # merge GTDB info with fasta+gff info
    return df_gtdb

def generate_thread_chunks (files, nthreads):
    n_files = len (files)
    if nthreads > n_files: nthreads = n_files
    chunk_size = n_files // nthreads + 1 
    file_chunks = [files[i:i+chunk_size] for i in range(0, n_files, chunk_size)]
    return file_chunks

