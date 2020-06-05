"""
reference:
https://zhuanlan.zhihu.com/p/49271699
https://jalammar.github.io/illustrated-bert/
"""

import tensorflow as tf
from tensorflow import keras
import utils
import time
from transformer import Encoder
import pickle
import os

MODEL_DIM = 32
N_LAYER = 3
BATCH_SIZE = 12
LEARNING_RATE = 1e-3


class BERT(keras.Model):
    def __init__(self, model_dim, max_len, n_layer, n_head, n_vocab, max_seg=3, drop_rate=0.1, padding_idx=0):
        super().__init__()
        self.padding_idx = padding_idx
        self.n_vocab = n_vocab
        self.max_len = max_len

        # I think task emb is not necessary for pretraining,
        # because the aim of all tasks is to train a universal sentence embedding
        # the body encoder is the same across all task, and the output layer defines each task.
        # finetuning replaces output layer and leaves the body encoder unchanged.

        # self.task_emb = keras.layers.Embedding(
        #     input_dim=n_task, output_dim=model_dim,  # [n_task, dim]
        #     embeddings_initializer=tf.initializers.RandomNormal(0., 0.01),
        # )

        self.word_emb = keras.layers.Embedding(
            input_dim=n_vocab, output_dim=model_dim,  # [n_vocab, dim]
            embeddings_initializer=tf.initializers.RandomNormal(0., 0.01),
        )

        self.segment_emb = keras.layers.Embedding(
            input_dim=max_seg, output_dim=model_dim,  # [max_seg, dim]
            embeddings_initializer=tf.initializers.RandomNormal(0., 0.01),
        )
        self.position_emb = keras.layers.Embedding(
            input_dim=max_len, output_dim=model_dim,  # [step, dim]
            embeddings_initializer=tf.initializers.RandomNormal(0., 0.01),
        )
        self.position_emb = self.add_weight(
            name="pos", shape=[max_len, model_dim], dtype=tf.float32,
            initializer=keras.initializers.RandomNormal(0., 0.01))
        self.position_space = tf.ones((1, max_len, max_len))
        self.encoder = Encoder(n_head, model_dim, drop_rate, n_layer)
        self.o_mlm = keras.layers.Dense(n_vocab)
        self.o_nsp = keras.layers.Dense(2)

        self.cross_entropy = keras.losses.SparseCategoricalCrossentropy(from_logits=True)
        self.opt = keras.optimizers.Adam(LEARNING_RATE)

    def __call__(self, seqs, segs, training=False):
        embed = self.input_emb(seqs, segs)  # [n, step, dim]
        z = self.encoder(embed, training=training, mask=self.self_mask(seqs))
        mlm_logits = self.o_mlm(z)  # [n, step, n_vocab]
        nsp_logits = self.o_nsp(tf.reshape(z, [z.shape[0], -1]))  # [n, n_cls]
        return mlm_logits, nsp_logits

    def step(self, seqs, segs, seqs_, nsp_labels):
        with tf.GradientTape() as tape:
            mlm_logits, nsp_logits = self(seqs, segs, training=True)
            mlm_loss = self.cross_entropy(seqs_, mlm_logits)
            nsp_loss = self.cross_entropy(nsp_labels, nsp_logits)
            loss = mlm_loss + 0.1 * nsp_loss
        grads = tape.gradient(loss, self.trainable_variables)
        self.opt.apply_gradients(zip(grads, self.trainable_variables))
        return loss, mlm_logits

    def input_emb(self, seqs, segs):
        return self.word_emb(seqs) + self.segment_emb(segs) + tf.matmul(
            self.position_space, self.position_emb)  # [n, step, dim]

    def self_mask(self, seqs):
        """
         abcd--
        b010011
        c001011
        d000111
        -000011
        -000001
        """
        eye = tf.eye(self.max_len+1, batch_shape=[len(seqs)], dtype=tf.float32)[:, 1:, :-1]
        pad = tf.math.equal(seqs, self.padding_idx)
        mask = tf.where(pad[:, tf.newaxis, tf.newaxis, :], 1, eye[:, tf.newaxis, :, :])
        return mask  # [n, 1, step, step]

    @property
    def attentions(self):
        attentions = {
            "encoder": [l.mh.attention.numpy() for l in self.encoder.ls],
        }
        return attentions


def main():
    # get and process data
    data = utils.MRPCData("./MRPC")
    print("num word: ", data.num_word)
    model = BERT(
        model_dim=MODEL_DIM, max_len=data.max_len-1, n_layer=N_LAYER, n_head=4, n_vocab=data.num_word,
        max_seg=data.num_seg, drop_rate=0.2, padding_idx=data.pad_id)
    t0 = time.time()
    for t in range(2500):
        seqs, segs, xlen, nsp_labels = data.sample(BATCH_SIZE)
        loss, pred = model.step(seqs[:, :-1], segs[:, :-1], seqs[:, 1:], nsp_labels)
        if t % 50 == 0:
            pred = pred[0].numpy().argmax(axis=1)
            t1 = time.time()
            print(
                "\n\nstep: ", t,
                "| time: %.2f" % (t1 - t0),
                "| loss: %.3f" % loss.numpy(),
                "\n| tgt: ", " ".join([data.i2v[i] for i in seqs[0, 1:][:xlen[0].sum()+1]]),
                "\n| prd: ", " ".join([data.i2v[i] for i in pred[:xlen[0].sum()+1]]),
                )
            t0 = t1
    os.makedirs("./visual_helper/bert", exist_ok=True)
    model.save_weights("./visual_helper/bert/model.ckpt")


def export_attention():
    data = utils.MRPCData("./MRPC")
    print("num word: ", data.num_word)
    model = BERT(
        model_dim=MODEL_DIM, max_len=data.max_len-1, n_layer=N_LAYER, n_head=4, n_vocab=data.num_word,
        max_seg=data.num_seg, drop_rate=0, padding_idx=data.pad_id)
    model.load_weights("./visual_helper/bert/model.ckpt")

    # save attention matrix for visualization
    seqs, segs, xlen, nsp_labels = data.sample(32)
    model(seqs[:, :-1], segs[:, :-1], False)
    data = {"src": [[data.i2v[i] for i in seqs[j]] for j in range(len(seqs))], "attentions": model.attentions}
    with open("./visual_helper/bert_attention_matrix.pkl", "wb") as f:
        pickle.dump(data, f)


if __name__ == "__main__":
    main()
    export_attention()

