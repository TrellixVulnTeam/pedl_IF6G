#!/usr/bin/env python

import argparse
import logging
import os
import sys
from collections import defaultdict
from pathlib import Path

from torch import nn
from tqdm import tqdm
import torch
from transformers import BertTokenizerFast

from pedl.model import BertForDistantSupervision
from pedl.dataset import PEDLDataset
from pedl.utils import DataGetter, get_geneid_to_name, build_summary_table, \
    get_hgnc_symbol_to_gene_id, chunks, Entity


def summarize(args):
    if not args.out:
        file_out = (args.path_to_files.parent / args.path_to_files.name).with_suffix(".tsv")
    else:
        file_out = args.out
    with open(file_out, "w") as f:
        f.write(f"p1\tassociation type\tp2\tscore (sum)\tscore (max)\n")
        for row in build_summary_table(args.path_to_files, score_cutoff=args.cutoff,
                                       no_association_type=args.no_association_type):
            f.write(f"{row[0]}\t{row[1]}\t{row[2]}\t{row[3]:.2f}\t{row[4]:.2f}\n")


def predict(args):

    hgnc_to_gene_id = get_hgnc_symbol_to_gene_id()

    if args.verbose:
        logging.basicConfig(level=logging.INFO)

    if len(args.p1) == 1 and os.path.exists(args.p1[0]):
        with open(args.p1[0]) as f:
            p1s = f.read().strip().split("\n")
    else:
        p1s = args.p1

    if len(args.p2) == 1 and os.path.exists(args.p2[0]):
        with open(args.p2[0]) as f:
            p2s = f.read().strip().split("\n")
    else:
        p2s = args.p2


    maybe_mapped_p1s = []
    for p1 in p1s:
        if not p1.isnumeric():
            assert p1 in hgnc_to_gene_id, f"{p1} is neither a valid HGNC symbol nor a Entrez gene id"
            maybe_mapped_p1s.append(hgnc_to_gene_id[p1])
        else:
            maybe_mapped_p1s.append(p1)

    maybe_mapped_p2s = []
    for p2 in p2s:
        if not p2.isnumeric():
            assert p2 in hgnc_to_gene_id, f"{p2} is neither a valid HGNC symbol nor a Entrez gene id"
            maybe_mapped_p2s.append(hgnc_to_gene_id[p2])
        else:
            maybe_mapped_p2s.append(p2)

    pairs_to_query = []
    for p1 in maybe_mapped_p1s:
        for p2 in maybe_mapped_p2s:
            pairs_to_query.append((p1, p2))
            if not args.skip_reverse:
                pairs_to_query.append((p2, p1))
    if len(pairs_to_query) > 100 and not args.pubtator:
        print(f"Using PEDL without a local PubTator copy is only supported for small queries up to 100 protein pairs. Your query contains {len(pairs_to_query)} pairs. Aborting.")
        sys.exit(1)

    if not args.device:
        if torch.cuda.is_available():
            args.device = "cuda"
        else:
            args.device = "cpu"

    model = BertForDistantSupervision.from_pretrained(args.model)
    if "cuda" in args.device:
        model.bert = nn.DataParallel(model.bert)
    model.eval()
    model.to(args.device)

    universe = set(maybe_mapped_p1s + maybe_mapped_p2s)
    data_getter = DataGetter(universe, local_pubtator=args.pubtator,
                             api_fallback=args.api_fallback,
                             expand_species=args.expand_species
                             )
    tokenizer = BertTokenizerFast.from_pretrained(args.model)
    tokenizer.add_special_tokens({ 'additional_special_tokens': ['<e1>','</e1>', '<e2>', '</e2>'] +
                                                                [f'<protein{i}/>' for i in range(1, 47)]})
    model.config.e1_id = tokenizer.convert_tokens_to_ids("<e1>")
    model.config.e2_id = tokenizer.convert_tokens_to_ids("<e2>")

    geneid_to_name = get_geneid_to_name()

    os.makedirs(args.out, exist_ok=True)

    if args.dbs:
        try:
            from pedl.database import PathwayCommonsDB
        except ImportError:
            print("Harvesting interactions from databases requires indra."
                  "Please install via `pip install indra' for obtaining additional interactions from databases.")
            sys.exit(1)
        logging.info("Preparing databases")
        dbs = [PathwayCommonsDB(i, gene_universe=universe) for i in args.dbs]
    else:
        dbs = []


    pbar = tqdm(total=len(pairs_to_query))
    for p1, p2 in pairs_to_query:
        name1 = geneid_to_name.get(p1, p1)
        name2 = geneid_to_name.get(p2, p2)
        pbar.set_description(f"{name1}-{name2}")
        if p1 == p2:
            pbar.update()
            continue

        processed_db_results = set()
        path_out = args.out / f"{name1}-{name2}.txt"
        with open(path_out, "w") as f:
            for db in dbs:
                stmts = db.get_statements(p1, p2)
                for stmt in stmts:
                    pmids = ",".join(i.pmid for i in stmt.evidence)
                    label = stmt.__class__.__name__
                    provenances = ",".join(i.source_id for i in stmt.evidence)
                    db_result = f"{label}\t1.0\t{pmids}\t{provenances}\t{db.name}\n\n"
                    if db_result not in processed_db_results:
                        f.write(db_result)
                    processed_db_results.add(db_result)

            probs = []
            all_sentences = []
            processed_sentences = set()

            for sentences_chunk in data_getter.get_sentences(p1 ,p2):
                sentences = sorted(sentences_chunk, key=lambda x: len(x.text))
                sentences = [i for i in sentences if i.text not in processed_sentences]
                processed_sentences.update(i.text for i in sentences)
                all_sentences += sentences
                for sentences_batch in chunks(sentences, args.batch_size):
                    tensors = tokenizer.batch_encode_plus([i.text_blinded for i in sentences_batch],
                                                          truncation=True, max_length=512)
                    max_length = max(len(i) for i in tensors["input_ids"])
                    tensors = tokenizer.pad(tensors, max_length=max_length,
                                            return_tensors="pt")
                    input_ids = tensors["input_ids"].to(args.device)
                    attention_mask = tensors["attention_mask"].to(args.device)
                    with torch.no_grad():
                        x, meta = model(input_ids, attention_mask)
                    probs_batch = torch.sigmoid(meta["alphas_by_rel"])
                    probs.append(probs_batch)

            if not all_sentences:
                pbar.update()
                if os.path.getsize(path_out) == 0:
                    os.remove(path_out)
                continue

            probs = torch.cat(probs)

            if (probs < args.cutoff).all():
                pbar.update()
                if os.path.getsize(path_out) == 0:
                    os.remove(path_out)
                continue

            for max_score in torch.sort(probs.view(-1), descending=True)[0]:
                if max_score.item() < args.cutoff:
                    continue
                for i, j in zip(*torch.where(probs == max_score)):
                    label = PEDLDataset.id_to_label[j.item()]
                    sentence = all_sentences[i]
                    f.write(f"{label}\t{max_score.item():.2f}\t{sentence.pmid}\t{sentence.text}\tPEDL\n\n")

        pbar.update()
        if os.path.getsize(path_out) == 0:
            os.remove(path_out)


def build_training_set(args):
    pair_to_relations = defaultdict(set)
    pmid_to_pairs = defaultdict(set)

    gene_universe = set()
    chemical_universe = set()

    with args.triples.open() as f:
        for line in f:
            fields = line.strip().split("\t")
            if fields:
                type_head, cuid_head, type_tail, cuid_tail, rel = fields
                head = Entity(cuid=cuid_head, type=type_head)
                tail = Entity(cuid=cuid_tail, type=type_tail)
                pair_to_relations[(head, tail)].add(rel)

                if head.type == "Chemical":
                    chemical_universe.add(head.cuid)
                elif head.type == "Gene":
                    gene_universe.add(head.cuid)
                else:
                    raise ValueError(head.type)
                if tail.type == "Chemical":
                    chemical_universe.add(tail.cuid)
                elif tail.type == "Gene":
                    gene_universe.add(tail.cuid)
                else:
                    raise ValueError(tail.type)

    data_getter = DataGetter(chemical_universe=chemical_universe,
                             gene_universe=gene_universe,
                             local_pubtator=args.pubtator,
                             expand_species=args.expand_species)

    all_pmids = set()

    for pair, relations in tqdm(list(pair_to_relations.items()),
                                desc="Preparing Crawl"):
        head, tail = pair[:2]
        shared_pmids = data_getter.get_pmids(head) & data_getter.get_pmids(tail)

        all_pmids.update(shared_pmids)
        for pmid in shared_pmids:
            pmid_to_pairs[pmid].add((head, tail))

    with open(str(args.out) + "." + str(args.worker_id), "w") as f, open(str(args.out_blinded) + "." + str(args.worker_id), "w") as f_blinded:
        for i, pmid in enumerate(tqdm(sorted(pmid_to_pairs), desc="Crawling")):
            if not (i % args.n_worker) == args.worker_id:
                continue
            pairs = pmid_to_pairs[pmid]
            docs = list(data_getter.get_documents([pmid]))[0]
            if docs:
                doc = docs[0]
                for head, tail in pairs:
                    sentences = data_getter.get_sentences_from_document(entity1=head,
                                                                        entity2=tail,
                                                                        document=doc)
                    relations = pair_to_relations[(head, tail)]
                    for sentence in sentences:
                        f.write("\t".join([head.type, head.cuid, tail.type, tail.cuid, ",".join(relations), sentence.text, sentence.pmid]) + "\n")
                        f_blinded.write("\t".join([head.type, head.cuid, tail.type, tail.cuid, ",".join(relations), sentence.text_blinded, sentence.pmid]) + "\n")



def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()

    parser_predict = subparsers.add_parser("predict")

    parser_predict.add_argument('--p1', required=True, nargs="+")
    parser_predict.add_argument('--p2', required=True, nargs="+")
    parser_predict.add_argument('--out', type=Path, required=True)
    parser_predict.add_argument('--model', default="leonweber/PEDL")
    parser_predict.add_argument('--dbs', nargs="*", choices=["pid", "reactome", "netpath",
                                                     "kegg", "panther", "humancyc"])
    parser_predict.add_argument('--pubtator', type=Path)
    parser_predict.add_argument('--device', default=None)
    parser_predict.add_argument('--topk', type=int, default=None)
    parser_predict.add_argument('--cutoff', type=float, default=0.01)
    parser_predict.add_argument('--batch_size', type=int, default=50)
    parser_predict.add_argument('--api_fallback', action="store_true")
    parser_predict.add_argument('--skip_reverse', action="store_true")
    parser_predict.add_argument('--verbose', action="store_true")
    parser_predict.add_argument('--expand_species', nargs="*")
    parser_predict.add_argument('--multi_sentence', action="store_true")
    parser_predict.set_defaults(func=predict)

    parser_summarize = subparsers.add_parser("summarize")
    parser_summarize.set_defaults(func=summarize)

    parser_summarize.add_argument("path_to_files", type=Path)
    parser_summarize.add_argument("--out", type=Path, default=None)
    parser_summarize.add_argument("--cutoff", type=float, default=0.0)
    parser_summarize.add_argument('--no_association_type', action="store_true")


    ## Build Training Data
    parser_build_training_set = subparsers.add_parser("build_training_set")
    parser_build_training_set.set_defaults(func=build_training_set)

    parser_build_training_set.add_argument("--out", type=Path, required=True)
    parser_build_training_set.add_argument("--out_blinded", type=Path, required=True)
    parser_build_training_set.add_argument('--expand_species', nargs="*")
    parser_build_training_set.add_argument("--n_worker", default=1, type=int)
    parser_build_training_set.add_argument("--worker_id", default=0, type=int)
    parser_build_training_set.add_argument("--triples", type=Path, required=True)
    parser_build_training_set.add_argument('--pubtator', type=Path)

    args = parser.parse_args()

    args.func(args)

if __name__ == '__main__':
    main()