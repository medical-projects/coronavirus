import itertools
import os

import numpy as np
import redis
from Bio import AlignIO, SeqIO
from Bio.Seq import Seq
from sklearn.neighbors import BallTree


def bytesu(string):
    return bytes(string, "UTF-8")


# length of the CRISPR guide RNA
K = 28
# path to a fasta file with host sequences to avoid
HOST_FILE = "GCF_000001405.39_GRCh38.p13_rna.fna"  # all RNA in human transcriptome
# HOST_FILE = "lung-tissue-gene-cds.fa" # just lungs
HOST_PATH = os.path.join("host", HOST_FILE)
# ending token for tries
END = bytesu("*")
EMPTY = bytesu('')
# path to pickle / save the trie
REBUILD_TRIE = False
TRIE_PATH = "trie"
# path to alignment and id for the sequence to delete
TARGET_PATH = os.path.join("alignments", "HKU1+MERS+SARS+nCoV-Consensus.clu")
TARGET_ID = "nCoV"
# mismatch cutoff (e.g. how close must two kmers be to bind?)
CUTOFF = 2
# bases 15-21 of Cas13 gRNA don't tolerate mismatch (Wessels et al 2019)
OFFSET_1, OFFSET_2 = 14, 21
# paths to plasmid part fasta files
PROMOTER_PATH = os.path.join("parts", "pdpn_1_promoter.fa")
DR_SEQUENCE_PATH = os.path.join("parts", "crispr", "dr_sequence_bz_short.fa")
TAIL_PATH = os.path.join("parts", "tail.fa")
# path for output
OUTFILE_PATH = os.path.join("guides", "trie_guides.csv")
BASE_A = 0
BASE_C = 1
BASE_G = 2
BASE_T = 3
WILDCARD_EXPANSION = {
    'a': {BASE_A},
    'c': {BASE_C},
    'g': {BASE_G},
    't': {BASE_T},
    'w': {BASE_A, BASE_T},
    'n': {BASE_A, BASE_C, BASE_G, BASE_T}
}

r = None
leveldb = None


def all_equal(arr):
    return arr.count(arr[0]) == len(arr)


def read_fasta(fasta_path: str) -> Seq:
    record = SeqIO.read(handle=fasta_path, format="fasta")
    return record.seq.lower()


def write_fasta(fasta_path: str, sequences):
    SeqIO.write(sequences=sequences, handle=fasta_path, format="fasta")


def getKmers(sequence: str, k: int, step: int):
    for x in range(0, len(sequence) - k + 1, step):
        yield sequence[x:x + k]


def kmer2vecs(kmer: str):
    return itertools.product(*[WILDCARD_EXPANSION[base] for base in kmer])


def host_has(kmer: str, tree: BallTree, max_mismatches=CUTOFF, k=K):
    distance, closest = tree.query(np.asarray(list(kmer2vecs(kmer))), 1, return_distance=True)
    print(f"closest to {kmer} is {distance}, which is {closest}")
    if distance > max_mismatches / k:
        print(f"allow {kmer}")
        return False
    print(f"avoid {kmer} matches {closest}")
    return True


def make_hosts(input_path=HOST_PATH, k=K):
    with open(input_path, "r") as host_file:
        return BallTree(np.asarray([vec
                     for record in SeqIO.parse(host_file, "fasta")
                     for kmer in getKmers(record.seq.lower(), k=k, step=1)
                     for vec in kmer2vecs(kmer)]), metric='hamming')


def make_targets(db=r, target_path=TARGET_PATH, target_id=TARGET_ID, k=K):
    targets_key = f"targets_{k}"
    alignment = AlignIO.read(target_path, "clustal")
    seq_ids = [seq.id for seq in alignment]
    index_of_target = seq_ids.index(target_id)
    alignment_length = alignment.get_alignment_length()
    conserved = conserved_in_alignment(alignment, alignment_length)
    for start in range(alignment_length - k + 1):
        kmer, n_conserved = count_conserved(alignment, conserved, index_of_target, start, k)
        if n_conserved > 0:
            print(f"{kmer} at {start} has {int(n_conserved)} conserved bases")
            db.zadd(targets_key, {kmer: n_conserved})
    most = db.zrevrangebyscore(targets_key, 9001, 0, withscores=True, start=0, num=1)[0]
    print(
        f"the most conserved {K}mer {most[0].decode()} has {int(most[1])} bases conserved in {seq_ids}")


def conserved_in_alignment(alignment, alignment_length):
    return [1 if all_equal(
        [seq[i] for seq in alignment]) else 0 for i in range(alignment_length)]


def count_conserved(alignment, conserved, index_of_target, start, k=K):
    if not all(conserved[start + OFFSET_1:start + OFFSET_2]):
        return "", 0
    else:
        kmer = str(alignment[index_of_target][start:start + k].seq).lower()
        if "-" in kmer:
            return "", 0
        n_conserved = sum(conserved[start:start + k])
    return kmer, n_conserved


def predict_side_effects(tree, db=r, out_path=OUTFILE_PATH, k=K, max_mismatches=CUTOFF):
    targets_key = f"targets_{k}"
    good_targets_key = f"good_targets_{k}"
    targets = db.zrevrangebyscore(targets_key, 9001, 0)
    for target in targets:
        t = target.decode()
        should_avoid = host_has(t, tree, max_mismatches, k)
        if should_avoid:
            continue
        db.zadd(good_targets_key, {t: db.zscore(targets_key, t)})
    with open(out_path, "w+") as outfile:
        for k, good_target in enumerate(db.zrevrangebyscore(good_targets_key, 90, 0)):
            good_target_string = good_target.decode()
            print("good target", k, good_target_string)
            outfile.write(good_target_string + "\n")
    print(f"saved {db.zcard(good_targets_key)} good targets at {out_path}")


if __name__ == "__main__":
    r = redis.Redis(host='localhost', port=6379)
    tree = make_hosts()
    # test the trie lookup works
    for i in range(5):
        host_has(r.srandmember("hosts").decode(), tree=tree)
    make_targets(db=r)
    predict_side_effects(db=r, tree=tree)
