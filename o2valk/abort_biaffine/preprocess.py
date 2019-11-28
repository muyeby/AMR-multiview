import configargparse
import glob
import os
import codecs
import gc

import torch
import torchtext.vocab
from collections import Counter, OrderedDict

import onmt.constants as Constants
import onmt.opts as opts
from inputters.dataset import get_fields, build_dataset, make_text_iterator_from_file
from utils.logging import init_logger, logger


def save_fields_to_vocab(fields):
    """
    Save Vocab objects in Field objects to `vocab.pt` file.
    """
    vocab = []
    for k, f in fields.items():
        if f is not None and 'vocab' in f.__dict__:
            f.vocab.stoi = f.vocab.stoi  # 返回单词和下标
            vocab.append((k, f.vocab))

    return vocab


def build_field_vocab(field, counter, **kwargs):  # *args表示任何多个无名参数，它是一个tuple；**kwargs表示关键字参数，它是一个 dict
    # fromkey()指定一个列表，把列表中的值作为字典的key,生成一个字典
    specials = list(OrderedDict.fromkeys(
        tok for tok in [field.unk_token, field.pad_token, field.init_token, field.eos_token]
        if tok is not None))
    field.vocab = field.vocab_cls(counter, specials=specials, **kwargs)


def merge_vocabs(vocabs, vocab_size=None, min_frequency=1):
    merged = sum([vocab.freqs for vocab in vocabs], Counter())
    return torchtext.vocab.Vocab(merged,
                                 specials=[Constants.UNK_WORD, Constants.PAD_WORD,
                                           Constants.BOS_WORD, Constants.EOS_WORD],
                                 max_size=vocab_size,
                                 min_freq=min_frequency)


def build_vocab(train_dataset_files, fields, share_vocab,
                src_vocab_size, src_words_min_frequency,
                tgt_vocab_size, tgt_words_min_frequency,
                structure_vocab_size, structure_words_min_frequency):
    counter = {}

    for k in fields:
        counter[k] = Counter()

    # Load vocabulary
    for _, path in enumerate(train_dataset_files):
        dataset = torch.load(path)
        logger.info(" * reloading %s." % path)
        for ex in dataset.examples:
            for k in fields:  # k: src、tgt、structure字段
                val = getattr(ex, k, None)
                if not fields[k].sequential:
                    continue
                if k == 'structure':
                    for i in val:
                        counter[k].update(i)
                else:
                    counter[k].update(val)

        dataset.examples = None
        gc.collect()
        del dataset.examples
        gc.collect()
        del dataset
        gc.collect()

    build_field_vocab(fields["tgt"], counter["tgt"],
                      max_size=tgt_vocab_size,
                      min_freq=tgt_words_min_frequency)
    logger.info(" * tgt vocab size: %d." % len(fields["tgt"].vocab))

    build_field_vocab(fields["src"], counter["src"],
                      max_size=src_vocab_size,
                      min_freq=src_words_min_frequency)
    logger.info(" * src vocab size: %d." % len(fields["src"].vocab))

    build_field_vocab(fields["structure"], counter["structure"],
                      max_size=structure_vocab_size,
                      min_freq=structure_words_min_frequency)
    logger.info(" * structure vocab size: %d." % len(fields["structure"].vocab))

    build_field_vocab(fields["mask"], counter["mask"],
                      max_size=2,
                      min_freq=0)
    logger.info(" * mask vocab size: %d." % len(fields["mask"].vocab))

    # Merge the input and output vocabularies.
    if share_vocab:
        # `tgt_vocab_size` is ignored when sharing vocabularies
        logger.info(" * merging src and tgt vocab...")
        merged_vocab = merge_vocabs(
            [fields["src"].vocab, fields["tgt"].vocab],
            vocab_size=src_vocab_size,
            min_frequency=src_words_min_frequency)
        fields["src"].vocab = merged_vocab
        fields["tgt"].vocab = merged_vocab
        logger.info(" * src vocab size: %d." % len(fields["src"].vocab))
        logger.info(" * tgt vocab size: %d." % len(fields["tgt"].vocab))

    return fields


def parse_args():
    parser = configargparse.ArgumentParser(
        description='preprocess.py',
        config_file_parser_class=configargparse.YAMLConfigFileParser,
        formatter_class=configargparse.ArgumentDefaultsHelpFormatter)

    opts.config_opts(parser)
    opts.preprocess_opts(parser)

    opt = parser.parse_args()
    torch.manual_seed(opt.seed)  ##为CPU设置种子用于生成随机数，以使得结果是确定的

    return opt


def build_save_in_shards_using_shards_size(src_corpus, tgt_corpus, structure_corpus, mask_corpus, fields, corpus_type, opt):
    src_data = []
    tgt_data = []
    structure_data = []
    mask_data = []
    with open(src_corpus, "r") as src_file:
        with open(tgt_corpus, "r") as tgt_file:
            with open(structure_corpus, "r") as structure_file:
                with open(mask_corpus, 'r') as mask_file:
                    for s, t, structure, mask in zip(src_file, tgt_file, structure_file, mask_file):
                        src_data.append(s)
                        tgt_data.append(t)
                        structure_data.append(structure)
                        mask_data.append(mask)

                        assert (len(s.split()) + 1) ** 2 == len(structure.split()) and (len(t.split()) ** 2 == len(mask.split()))

    if len(src_data) != len(tgt_data) or len(tgt_data) != len(structure_data):
        raise AssertionError("Source,Target and structure should have the same length")

    num_shards = int(len(src_data) / opt.shard_size)
    for x in range(num_shards):
        logger.info("Splitting shard %d." % x)

        f = codecs.open(src_corpus + ".{0}.txt".format(x), "w", encoding="utf-8")
        f.writelines(src_data[x * opt.shard_size: (x + 1) * opt.shard_size])
        f.close()

        f = codecs.open(tgt_corpus + ".{0}.txt".format(x), "w", encoding="utf-8")
        f.writelines(tgt_data[x * opt.shard_size: (x + 1) * opt.shard_size])
        f.close()

        f = codecs.open(structure_corpus + ".{0}.txt".format(x), "w", encoding="utf-8")
        f.writelines(structure_data[x * opt.shard_size: (x + 1) * opt.shard_size])
        f.close()

        f = codecs.open(mask_corpus + ".{0}.txt".format(x), "w", encoding="utf-8")
        f.writelines(mask_data[x * opt.shard_size: (x + 1) * opt.shard_size])
        f.close()

    num_written = num_shards * opt.shard_size
    if len(src_data) > num_written:  # 处理最后一个剩下的shard
        logger.info("Splitting shard %d." % num_shards)
        f = codecs.open(src_corpus + ".{0}.txt".format(num_shards), 'w', encoding="utf-8")
        f.writelines(src_data[num_shards * opt.shard_size:])
        f.close()

        f = codecs.open(tgt_corpus + ".{0}.txt".format(num_shards), 'w', encoding="utf-8")
        f.writelines(tgt_data[num_shards * opt.shard_size:])
        f.close()

        f = codecs.open(structure_corpus + ".{0}.txt".format(num_shards), 'w', encoding="utf-8")
        f.writelines(structure_data[num_shards * opt.shard_size:])
        f.close()

        f = codecs.open(mask_corpus + ".{0}.txt".format(num_shards), 'w', encoding="utf-8")
        f.writelines(mask_data[num_shards * opt.shard_size:])
        f.close()

    src_list = sorted(glob.glob(src_corpus + '.*.txt'))
    tgt_list = sorted(glob.glob(tgt_corpus + '.*.txt'))
    structure_list = sorted(glob.glob(structure_corpus + '.*.txt'))
    mask_list = sorted(glob.glob(mask_corpus + '.*.txt'))

    ret_list = []

    for index, src in enumerate(src_list):
        logger.info("Building shard %d." % index)
        src_iter = make_text_iterator_from_file(src)  # 迭代器，每次返回文件中的一行数据
        tgt_iter = make_text_iterator_from_file(tgt_list[index])
        structure_iter = make_text_iterator_from_file(structure_list[index])
        mask_iter = make_text_iterator_from_file(mask_list[index])

        dataset = build_dataset(
            fields,
            src_iter,
            tgt_iter,
            structure_iter,
            mask_iter,
            src_seq_length=opt.src_seq_length,
            tgt_seq_length=opt.tgt_seq_length,
            src_seq_length_trunc=opt.src_seq_length_trunc,
            tgt_seq_length_trunc=opt.tgt_seq_length_trunc
        )

        pt_file = "{:s}_{:s}.{:d}.pt".format(opt.save_data, corpus_type, index)  # ..../gq_coupus_type.{0,1}.pt

        # We save fields in vocab.pt seperately, so make it empty.
        dataset.fields = []

        logger.info(" * saving %sth %s data shard to %s." % (index, corpus_type, pt_file))
        torch.save(dataset, pt_file)
        ret_list.append(pt_file)

        os.remove(src)
        os.remove(tgt_list[index])
        os.remove(structure_list[index])
        os.remove(mask_list[index])
        del dataset.examples
        gc.collect()
        del dataset
        gc.collect()

    return ret_list  # 返回一个文件名列表


def store_vocab_to_file(vocab, filename):
    with open(filename, "w") as f:
        for i, token in enumerate(vocab.itos):
            # TEXT.vocab类的三个variables,freqs 用来返回每一个单词和其对应的频数  itos 按照下标的顺序返回每一个单词 stoi 返回每一个单词与其对应的下标
            f.write(str(i) + ' ' + token + '\n')
        f.close()


def build_save_vocab(train_dataset, fields, opt):
    """ Building and saving the vocab """
    fields = build_vocab(train_dataset,
                         fields,
                         opt.share_vocab,
                         opt.src_vocab_size,
                         opt.src_words_min_frequency,
                         opt.tgt_vocab_size,
                         opt.tgt_words_min_frequency,
                         opt.structure_vocab_size,
                         opt.structure_words_min_frequency)

    # Can't save fields, so remove/reconstruct at training time.
    vocab_file = opt.save_data + '_vocab.pt'
    torch.save(save_fields_to_vocab(fields), vocab_file)
    store_vocab_to_file(fields['src'].vocab, opt.save_data + '_src_vocab')
    store_vocab_to_file(fields['tgt'].vocab, opt.save_data + '_tgt_vocab')
    store_vocab_to_file(fields['structure'].vocab, opt.save_data + '_structure_vocab')
    store_vocab_to_file(fields['mask'].vocab, opt.save_data + '_mask_vocab')


def build_save_dataset(corpus_type, fields, opt):  # corpus_type: train or valid
    """ Building and saving the dataset """
    assert corpus_type in ['train', 'valid']  # Judging whether it is train or valid

    if corpus_type == 'train':
        src_corpus = opt.train_src  # 获取源端、目标端和结构信息的path
        tgt_corpus = opt.train_tgt
        structure_corpus = opt.train_structure
        mask_corpus=opt.train_mask
    else:
        src_corpus = opt.valid_src
        tgt_corpus = opt.valid_tgt
        structure_corpus = opt.valid_structure
        mask_corpus=opt.valid_mask

    if (opt.shard_size > 0):
        return build_save_in_shards_using_shards_size(src_corpus, tgt_corpus, structure_corpus, mask_corpus, fields, corpus_type,
                                                      opt)

    # We only build a monolithic dataset.
    # But since the interfaces are uniform, it would be not hard to do this should users need this feature.
    src_iter = make_text_iterator_from_file(src_corpus)
    tgt_iter = make_text_iterator_from_file(tgt_corpus)
    structure_iter = make_text_iterator_from_file(structure_corpus)
    mask_iter=make_text_iterator_from_file(mask_corpus)

    dataset = build_dataset(
        fields,
        src_iter,
        tgt_iter,
        structure_iter,
        mask_iter,
        src_seq_length=opt.src_seq_length,
        tgt_seq_length=opt.tgt_seq_length,
        src_seq_length_trunc=opt.src_seq_length_trunc,
        tgt_seq_length_trunc=opt.tgt_seq_length_trunc)

    # We save fields in vocab.pt seperately, so make it empty.
    dataset.fields = []

    pt_file = "{:s}_{:s}.pt".format(opt.save_data, corpus_type)
    logger.info(" * saving %s dataset to %s." % (corpus_type, pt_file))

    torch.save(dataset, pt_file)

    return [pt_file]


def main():
    opt = parse_args()
    if (opt.shuffle > 0):
        raise AssertionError("-shuffle is not implemented, please make sure \
                         you shuffle your data before pre-processing.")
    init_logger(opt.log_file)
    logger.info("Input args: %r", opt)
    logger.info("Extracting features...")

    logger.info("Building 'Fields' object...")
    fields = get_fields()

    logger.info("Building & saving training data...")
    train_dataset_files = build_save_dataset('train', fields, opt)  # 返回生成的文件列表

    logger.info("Building & saving validation data...")
    build_save_dataset('valid', fields, opt)

    logger.info("Building & saving vocabulary...")
    build_save_vocab(train_dataset_files, fields, opt)  # only用train集创建vocabulary


if __name__ == "__main__":
    main()
