import os, logging, xxhash
from Bio import Seq, SeqIO, AlignIO
import random, datetime, sys, re, glob, collections, subprocess, itertools, pathlib, base64, string
import lzma, gzip, bz2, dendropy, treeswift, copy, numpy as np
from sklearn import metrics

# legacy code, now every module shares the same parent logger
#log_format = logging.Formatter(fmt='phylobarcode_common %(asctime)s [%(levelname)s] %(message)s', datefmt="%Y-%m-%d %H:%M")
logger = logging.getLogger("phylobarcode_global_logger")

base62 = string.digits + string.ascii_letters + '_.-+~@'  # 66 elements actually (62 is alphanum only)
len_base62 = len (base62)

def remove_prefix_suffix (strlist):
    def all_same(x):  # https://stackoverflow.com/a/6719272/204903
        return all(x[0] == y for y in x)
    if isinstance(strlist, str): return ""
    if len(strlist) == 1: return ""
    char_tuples = zip(*strlist)
    fix_tuples  = itertools.takewhile(all_same, char_tuples)
    prefix = ''.join(x[0] for x in fix_tuples)
    inverse = [x[::-1] for x in strlist]
    char_tuples = zip(*inverse)
    fix_tuples  = itertools.takewhile(all_same, char_tuples)
    suffix = ''.join(x[0] for x in fix_tuples)
    suffix = suffix[::-1]

    l_pre = len(prefix) ## we could skip "prefix" and store lenght of fix_tuples but this is more readable
    l_suf = len(suffix)
    return [x[l_pre:len(x)-l_suf] for x in strlist] # does not work well for 'lefT' and 'righT' 

def split_gtdb_taxonomy_from_dataframe (taxon_df, gtdb_column = "gtdb_taxonomy", drop_gtdb_column = True, replace = None):
    '''
    Splits the GTDB taxonomy string into a list of taxonomic ranks: 
    d__Bacteria;p__Firmicutes;c__Bacilli;o__Bacillales;f__Bacillaceae_H;g__Priestia;s__Priestia megaterium
    '''
    linneus = {"phylum":1, "class":2, "order":3, "family":4, "genus":5, "species":6}
    for k,v in linneus.items():
        taxon_df[k] = taxon_df[gtdb_column].str.split(";").str[v].str.split("__").str[1]
    if replace is not None:
        for k in linneus.keys():
            taxon_df[k] = taxon_df[k].fillna(replace)
    if (drop_gtdb_column): taxon_df.drop(columns = [gtdb_column], inplace = True)
    return taxon_df

def seq_to_base62 (seq): # https://stackoverflow.com/questions/1119722/base-62-conversion
    '''
    Convert a sequence to a base62 string of its integer representation.
    '''
    integer = xxhash.xxh128_intdigest(seq) # alias of xxh3_128_intdigest()
    if integer == 0:
        return base62[0]
    ret = ''
    while integer != 0:
        ret = base62[integer % len_base62] + ret
        integer //= len_base62
    return ret

def read_fasta_as_list (filename, clean_sequence=True, substring=None):
    unaligned = []
    with open_anyformat (filename, "r") as handle:
        for record in SeqIO.parse(handle, "fasta"):
            if clean_sequence:
                record.seq  = Seq.Seq(str(record.seq.upper()).replace(".","N"))
            if substring is None or any([x in record.description for x in substring]):
                unaligned.append(record)
    logger.debug("Read %s sequences from file %s", str(len(unaligned)), filename)
    return unaligned

def read_fasta_headers_as_list (filename):
    seqnames = []
    with open_anyformat (filename, "r") as handle:
        for record in SeqIO.parse(handle, "fasta"):
            seqnames.append(record.description)
    logger.debug("Read %s sequence headers from file %s", str(len(seqnames)), filename)
    return seqnames

def mafft_align_seqs (sequences=None, infile = None, outfile = None, prefix = None, nthreads = 1): # list not dict
    if (sequences is None) and (infile is None):
        logger.error("You must give me a fasta object or a file")
        return None
    if prefix is None: prefix = "./"
    hash_name = '%012x' % random.randrange(16**12)
    if infile is None: ifl = f"{prefix}/mafft_{hash_name}.fasta"
    else: ifl = infile # if both infile and sequences are present, it will save (overwrite) infile
    if outfile is None: ofl = f"{prefix}/mafft_{hash_name}.aln"
    else: ofl = outfile # in this case it will not exclude_reference
    if sequences: SeqIO.write(sequences, ifl, "fasta") ## else it should be present in infile
    if nthreads < 1: nthreads = -1 # mafft default to use all available threads

    runstr = f"mafft --auto --ep 0.23 --leavegappyregion --thread {nthreads} {ifl} > {ofl}"
    try:
        proc_run = subprocess.check_output(runstr, shell=True, universal_newlines=True)
    except subprocess.CalledProcessError as e:
        logger.error("Error running mafft: %s", e)
        aligned = None
    else:
        aligned = AlignIO.read(ofl, "fasta")

    if infile is None:  os.remove(ifl)
    if outfile is None: os.remove(ofl)
    return aligned

def cdhit_cluster_seqs (sequences=None, infile = None, outfile = None, prefix = None, nthreads = 1, 
        id = 0.9, fast = True): # list not dict
    def read_clstr_file (clstr_file):
        clusters = {}
        with open(clstr_file, "r") as clstr:
            for line in clstr:
                if line.startswith(">"):
                    cluster_id = line.strip().split()[1] # e.g. ">Cluster 0"
                    clusters[cluster_id] = []
                else:
                    x = line.strip().split()[2].strip(">")
                    clusters[cluster_id].append(x[:x.index("...")]) # e.g. "0	100nt, >seq1... at 100%"
        clusters = [v for v in clusters.values()] # dict of lists to list of lists (we don't need cluster ids)
        return clusters

    if (sequences is None) and (infile is None):
        print ("ERROR: You must give me a fasta object or a file")
    if prefix is None: prefix = "./"
    hash_name = '%012x' % random.randrange(16**12)
    if infile is None: ifl = f"{prefix}/cdhit_{hash_name}.fasta"
    else: ifl = infile # if both infile and sequences are present, it will save (overwrite) infile
    if outfile is None: ofl = f"{prefix}/cdhit_{hash_name}.reps.fasta"
    else: ofl = outfile # in this case it will not exclude_reference
    if sequences: SeqIO.write(sequences, ifl, "fasta") ## else it should be present in infile
    if nthreads < 1: nthreads = 0 # cdhit default to use all available threads
    if fast is True: algo = "0" # sequence is clustered to the first cluster that meet the threshold
    else: algo = "1" # sequence is clustered to the most similar cluster that meet the threshold

    runstr = f"cd-hit -i {ifl} -o {ofl} -c {id} -M 0 -T {nthreads} -d 0 -aS 0.5 -aL 0.5 -g {algo} -s 0.5 -p 0"
    try:
        proc_run = subprocess.check_output(runstr, shell=True, universal_newlines=True)
    except subprocess.CalledProcessError as e:
        logger.error("Error running cdhit: %s", e)
        representatives = None
        clusters = None
    else:
        representatives = SeqIO.parse(ofl, "fasta")
        clusters = read_clstr_file(f"{ofl}.clstr")

    if infile is None:  os.remove(ifl)
    if outfile is None: os.remove(ofl)
    os.remove(f"{ofl}.clstr") ## always remove the clstr file
    return representatives, clusters

# In general (not here particularly) we use dendropy since it can handle commented nodes (metadata)
#   e.g. the GTDB tree
def silhouette_score_from_newick_dendropy (newick, class_dict):
    """
    returns silhouette scores for all tips in the tree as dictionaries: one considering 
    branch lengths and another considering only number of nodes
    """
    if isinstance (newick, str):
        tree = dendropy.Tree.get(data=newick, schema="newick", preserve_underscores=True)
    elif isinstance (newick, dendropy.Tree):
        tree = newick
    else: tree = dendropy.Tree.get(data=str(newick), schema="newick", preserve_underscores=True) # treeswift?
    
    species = [class_dict[x.label] for x in tree.taxon_namespace]
    ntaxa = len(species)
    distmat = np.zeros((ntaxa, ntaxa))
    nodemat = np.zeros((ntaxa, ntaxa))
    # STEP 1: pairwise distances along the tree
    pdm = tree.phylogenetic_distance_matrix()
    for i,j in itertools.combinations(range(ntaxa), 2):
        distmat[j,i] = distmat[i,j] = pdm.distance(tree.taxon_namespace[i], tree.taxon_namespace[j])
        nodemat[j,i] = nodemat[i,j] = pdm.path_edge_count(tree.taxon_namespace[i], tree.taxon_namespace[j])
    # STEP 2: silhouette score using pairwise distances and taxonomic information
    mdist = metrics.silhouette_samples(distmat, species, metric="precomputed")
    mnode = metrics.silhouette_samples(nodemat, species, metric="precomputed")
    mdist = {tree.taxon_namespace[i].label: mdist[i] for i in range(ntaxa)}
    mnode = {tree.taxon_namespace[i].label: mnode[i] for i in range(ntaxa)}
    return mdist, mnode # dictionaries with the silhouette score for each sequence

def silhouette_score_from_newick_swift (newick, class_dict):
    """ 
    returns silhouette scores for all tips in the tree as one dictionary, considering branch lengths.
    Much faster than the alternative function (if you want number of nodes)
    """
    if isinstance (newick, dendropy.Tree): tree = treeswift.read_tree_dendropy (newick)
    else: tree = treeswift.read_tree_newick (newick) # treeswift object _or_ string 
    for node in tree.traverse_leaves(): node.label = node.label.replace("'", "")
    labels = [x.label for x in tree.traverse_leaves() if x.label is not None]
    species = [class_dict[x] for x in labels]
    ntaxa = len(species)
    distmat = np.zeros((ntaxa, ntaxa))
    # STEP 1: pairwise distances along the tree
    dist_dict = tree.distance_matrix(leaf_labels=True) ## dictionary of dictionaries, with leaf labels (ow. node objects)
    for i,j in itertools.combinations(range(ntaxa), 2):
        distmat[j,i] = distmat[i,j] = dist_dict[labels[i]][labels[j]]
    # STEP 2: silhouette score using pairwise distances and taxonomic information
    mdist = metrics.silhouette_samples(distmat, species, metric="precomputed")
    mdist = {labels[i]: mdist[i] for i in range(ntaxa)}
    return mdist # dictionaries with the silhouette score for each sequence

def newick_string_from_alignment (sequences=None, infile = None, simple_names = None, outfile = None, prefix = None, 
        protein = False, rapidnj = None, nthreads = 1): 
    """
    rapidnj uses whole fasta header description, while fasttree uses only the sequence id;
    therefore to use rapidnj is advised to use simple_names=True and give _sequences_ and not _infile_
    """
    if (sequences is None) and (infile is None):
        logger.error("You must give me a fasta object or a file")
        return None
    if prefix is None: prefix = "./"
    hash_name = '%012x' % random.randrange(16**12)
    if rapidnj is True: program = "rapidnj"
    else: program = "fasttree" 
    if outfile is None: ofl = f"{prefix}/{program}_{hash_name}.tree"
    else: ofl = outfile # in this case it will not exclude_reference
    ifl = f"{prefix}/{program}_{hash_name}.fasta" #always generate a fasta file 

    # read infile since we need to simplify the names, and infile may be compressed
    if sequences is None: new_seqs = read_fasta_as_list (infile)
    else: new_seqs = sequences

    if simple_names is True: # create a copy of all SeqRecords with no description (i.e. long names after space) 
        if sequences is None: # then we have new_seqs which we can overwrite (o.w. careful not to overwtite sequences)
            for x in new_seqs: x.description = "" # remove the description
        else: new_seqs = [SeqRecord(Seq(str(s.seq)), id=s.id, description="") for i, s in enumerate(sequences)] # copy
    SeqIO.write(new_seqs, ifl, "fasta")
    if nthreads < 1: nthreads = 1 # rapidnj default to use 1 thread; fastree has no control (all or nothing)

    if program == "rapidnj":
        seqtype = "d" if protein is False else "p"
        runstr = f"rapidnj {ifl} -i fa -c {nthreads} -t {seqtype} -n -x {ofl}"
    else:
        seqtype = "-nt" if protein is False else ""
        runstr = f"fasttree {seqtype} -quiet -nni 4 -spr 4 -mlnni 2 -nocat -nosupport {ifl} > {ofl}"
    try:
        proc_run = subprocess.check_output(runstr, shell=True, universal_newlines=True)
    except subprocess.CalledProcessError as e:
        logger.error("Error running {program} %s", e)
        treestring = None
    else:
        treestring = open(ofl).readline().rstrip().replace("\'","").replace("\"","").replace("[&R]","")

    os.remove(ifl) ## always delete since it's created here
    if outfile is None: os.remove(ofl)
    return treestring

def calc_freq_N_from_string (genome):
    l = len(genome)
    if (l):
        number_Ns = sum([genome.upper().count(nuc) for nuc in ["N", "-"]])
        return number_Ns / l
    else: return 1.

def calc_freq_ACGT_from_string (genome):
    l = len(genome)
    if (l):
        number_ACGTs = sum([genome.upper().count(nuc) for nuc in ["A", "C", "G", "T"]])
        return number_ACGTs / l
    else: return 0.

def remove_duplicated_sequences_list (sequences): # input is list, returns a dict
    uniq_seqs = {}
    uniq_qual = {}
    duplicates = []
    for x in sequences:
        seq = str(x.seq)
        quality = len(seq) - sum([seq.upper().count(nuc) for nuc in ["N", "-"]])
        if x.id in uniq_seqs.keys(): # sequence name has been seen before 
            if uniq_qual[x.id] < quality: # replaces if better quality
                uniq_qual[x.id] = quality
                uniq_seqs[x.id] = x
            duplicates.append(x.id)
        else: # first time we see this sequence
            uniq_qual[x.id] = quality
            uniq_seqs[x.id] = x
    if len(duplicates)>0:
        logger.warning ("%s duplicate (i.e. with same name) sequences were resolved by choosing the one with highest quality", len(duplicates))
        duplicates = list(set(duplicates))
        logger.debug ("And the sequence names are:\n%s\n", "\n".join(duplicates))
    else:
        logger.info ("Checked for duplicates but all sequences have distinct names")
    return uniq_seqs, uniq_qual

def save_sequence_dict_to_file (seqs, fname=None, use_seq_id = False):
    if fname is None: fname = "tmp." + '%012x' % random.randrange(16**12) + ".aln.xz"
    logger.info(f"Saving sequences to file {fname}")
    with open_anyformat (fname, "w") as fw: 
        for name, rec in seqs.items():
            if use_seq_id is True: # default is to use dict key
                name = rec.id
            if rec:  ## missing/query sequences
                seq = str(rec.seq)
                fw.write(str(f">{name}\n{seq}\n").encode())
                rec.id = name ## make sure alignment will have same names
    logger.info(f"Finished saving sequences")
    return os.path.basename(fname)

def open_anyformat (fname, mode = "r"):
    if (mode == "r"): openmode = "rt"
    else:             openmode = "wb"
    if   fname.endswith(".bz2"): this_open = bz2.open #if "bz2" in filename[-5:]: this_open = bz2.open
    elif fname.endswith(".gz"):  this_open = gzip.open
    elif fname.endswith(".xz"):  this_open = lzma.open
    else:  
        this_open = open
#      if (mode == "w"): openmode = "w"  ## only raw file for writting doesn't need "wb"
    return this_open (fname, openmode) 

