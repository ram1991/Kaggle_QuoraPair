import tensorflow as tf
from tensorflow.python.ops import tensor_array_ops, control_flow_ops


class TARGET_LSTM(object):
    def __init__(self, num_emb, batch_size, emb_dim, hidden_dim, sequence_length, start_token, params):
        self.num_emb = num_emb
        self.batch_size = batch_size
        self.emb_dim = emb_dim
        self.hidden_dim = hidden_dim
        self.sequence_length = sequence_length
        self.start_token = tf.constant([start_token] * self.batch_size, dtype=tf.int32)
        self.g_params = []
        self.temperature = 1.0
        self.params = params

        tf.set_random_seed(66)

        with tf.variable_scope('generator'):
            self.g_embeddings = tf.Variable(self.params[0])
            self.g_params.append(self.g_embeddings)
            self.g_recurrent_unit = self.create_recurrent_unit(self.g_params)  # maps h_tm1 to h_t for generator
            self.g_output_unit = self.create_output_unit(self.g_params)  # maps h_t to o_t (output token logits)

        #
        # placeholder definition
        # ----------------------------------------------------------------------------
        # sequence of tokens generated by generator
        self.x = tf.placeholder(tf.int32, shape=[self.batch_size, self.sequence_length])
        # ----------------------------------------------------------------------------

        #
        # processed for batch
        # ----------------------------------------------------------------------------
        # dim(self.processed_x) = (seq_length, batch_size, emb_dim)
        with tf.device("/cpu:0"):
            self.processed_x = tf.transpose(tf.nn.embedding_lookup(self.g_embeddings, self.x), perm=[1, 0, 2])
        # ----------------------------------------------------------------------------

        #
        # Initial states
        # ----------------------------------------------------------------------------
        self.h0 = tf.zeros([self.batch_size, self.hidden_dim])
        self.h0 = tf.stack([self.h0, self.h0])
        # ----------------------------------------------------------------------------

        #
        # generator on initial randomness
        # ----------------------------------------------------------------------------
        gen_o = tensor_array_ops.TensorArray(dtype=tf.float32, size=self.sequence_length,
                                             dynamic_size=False, infer_shape=True)
        gen_x = tensor_array_ops.TensorArray(dtype=tf.int32, size=self.sequence_length,
                                             dynamic_size=False, infer_shape=True)
        # ----------------------------------------------------------------------------

        def _g_recurrence(i, x_t, h_tm1, gen_o, gen_x):
            # Def:
            #   LSTM forward operation unit, where output at (t-1) will be sent as input at t
            #   This function is used prediction time slice from (t+1) to T
            # Args ------------
            #   i: counter
            #   x_t: input at time t
            #   h_tm1: a tensor that packs [prev_hidden_state, prev_c], i.e., h_{t-1}
            #   gen_o:
            #   gen_x: to record each predicted input from t to T
            # Returns ------------
            #   i + 1: next counter
            #   x_tp1: input at time (t+1), i.e., x_{t+1}, which is from next_token, the output from o_t
            #   h_t: a tensor that packs [now_hidden_state, now_c], i.e., h_{t}
            #   gen_o:
            #   gen_x: add next_token to the list, i.e., to record each predicted input from (t+1) to T

            # hidden_memory_tuple
            # h_tm1: the previous tensor that packs [prev_hidden_state, prev_c]
            # h_t: the current tensor that packs [now_hidden_state, now_c]
            h_t = self.g_recurrent_unit(x_t, h_tm1)

            # dim(o_t) = (batch_size, num_vocab), logits not prob
            # h_t: the current tensor that packs [now_hidden_state, now_c]
            # o_t: the output of LSTM at time t
            o_t = self.g_output_unit(h_t)

            log_prob = tf.log(tf.nn.softmax(o_t))
            next_token = tf.cast(tf.reshape(tf.multinomial(log_prob, 1), [self.batch_size]), tf.int32)

            # Convert next_token (vocabularies) to embeddings (next input, i.e., x_{t+1})
            # dim(x_tp1) = (batch_size, embed_dim)
            x_tp1 = tf.nn.embedding_lookup(self.g_embeddings, next_token)

            # dim(gen_o) = (batch_size, num_vocab), prob. dist. on vocab. vector
            # e.g., [3, 2, 1] == softmax ==> [0.665, 0.244, 0.09] == * one_hot ==> [0.665, 0, 0]
            # reduce_sum(input_tensor, axis=1), row-wise summation
            tmp = tf.multiply(tf.one_hot(next_token, self.num_vocab, 1.0, 0.0), tf.nn.softmax(o_t))
            gen_o = gen_o.write(i, tf.reduce_sum(tmp, 1))

            # dim(gen_x) = (indices, batch_size)
            gen_x = gen_x.write(i, next_token)

            return i + 1, x_tp1, h_t, gen_o, gen_x

        _, _, _, self.gen_o, self.gen_x = control_flow_ops.while_loop(cond=lambda i, _1, _2, _3, _4: i < self.sequence_length,
                                                                      body=_g_recurrence,
                                                                      loop_vars=(tf.constant(0, dtype=tf.int32),
                                                                                 tf.nn.embedding_lookup(self.g_embeddings, self.start_token),
                                                                                 self.h0, gen_o, gen_x))
        # dim(self.gen_x) = (seq_length, batch_size)
        self.gen_x = self.gen_x.stack()

        # dim(self.gen_x) = (batch_size, seq_length)
        self.gen_x = tf.transpose(self.gen_x, perm=[1, 0])

        # supervised pre-training for generator
        g_predictions = tensor_array_ops.TensorArray(dtype=tf.float32,
                                                     size=self.sequence_length,
                                                     dynamic_size=False,
                                                     infer_shape=True)

        #
        # Forward prediction to predict the sequence from 0 to t (predict by known instances)
        # ----------------------------------------------------------------------------
        # The input from 0 to t
        ta_emb_x = tensor_array_ops.TensorArray(dtype=tf.float32,
                                                size=self.sequence_length)
        ta_emb_x = ta_emb_x.unstack(self.processed_x)

        def _pretrain_recurrence(i, x_t, h_tm1, g_predictions):
            # Def:
            #   LSTM forward operation unit, given input and output
            #   This function is used prediction time slice from 1 to t
            # Args ------------
            #   i: counter
            #   x_t: input at time t
            #   h_tm1: a tensor that packs [prev_hidden_state, prev_c], i.e., h_{t-1}
            #   g_predictions: add softmax(o_t) to the list, i.e., to record each predicted input from t to T
            # Returns ------------
            #   i + 1: next counter
            #   x_tp1: input at time (t+1), i.e., x_{t+1}, which is read from ta_emb_x
            #   h_t: a tensor that packs [now_hidden_state, now_c], i.e., h_{t}
            #   g_predictions: add softmax(o_t) to the list, i.e., to record each predicted input from t to T
            h_t = self.g_recurrent_unit(x_t, h_tm1)
            o_t = self.g_output_unit(h_t)
            g_predictions = g_predictions.write(i, tf.nn.softmax(o_t))  # batch x vocab_size
            x_tp1 = ta_emb_x.read(i)
            return i + 1, x_tp1, h_t, g_predictions

        _, _, _, self.g_predictions = control_flow_ops.while_loop(cond=lambda i, _1, _2, _3: i < self.sequence_length,
                                                                  body=_pretrain_recurrence,
                                                                  loop_vars=(tf.constant(0, dtype=tf.int32),
                                                                             tf.nn.embedding_lookup(self.g_embeddings, self.start_token),
                                                                             self.h0, g_predictions))

        # dim(self.g_predictions) = (batch_size, seq_length, vocab_size)
        self.g_predictions = tf.transpose(self.g_predictions.stack(), perm=[1, 0, 2])
        # ----------------------------------------------------------------------------

        #
        # Pre-training loss: dim(self.out_loss) = scale, i.e., (1)
        # ----------------------------------------------------------------------------
        # dim(tmp1) = (len(self.x), self.num_vocab) <=== self.x
        tmp1 = tf.one_hot(tf.to_int32(tf.reshape(self.x, [-1])), self.num_emb, 1.0, 0.0)
        # dim(tmp2) = (len(self.x), self.num_vocab) <=== self.g_predictions
        tmp2 = tf.log(tf.reshape(self.g_predictions, [-1, self.num_emb]))
        self.pretrain_loss = -tf.reduce_sum(tmp1 * tmp2) / (self.sequence_length * self.batch_size)
        # ----------------------------------------------------------------------------

        #
        # Output loss: dim(self.out_loss) = (batch_size)
        # ----------------------------------------------------------------------------
        # dim(tmp1) = (len(self.x), self.num_vocab) <=== self.x
        tmp1 = tf.one_hot(tf.to_int32(tf.reshape(self.x, [-1])), self.num_emb, 1.0, 0.0)
        # dim(tmp2) = (len(self.x), self.num_vocab) <=== self.g_predictions
        tmp2 = tf.log(tf.reshape(self.g_predictions, [-1, self.num_emb]))
        self.out_loss = tf.reduce_sum(tf.reshape(-tf.reduce_sum(tmp1 * tmp2, 1), [-1, self.sequence_length]), 1)
        # ----------------------------------------------------------------------------

    def generate(self, session):
        # h0 = np.random.normal(size=self.hidden_dim)
        outputs = session.run(self.gen_x)
        return outputs

    def init_matrix(self, shape):
        return tf.random_normal(shape, stddev=1.0)

    def create_recurrent_unit(self, params):
        # Weights and Bias for input and hidden tensor
        self.Wi = tf.Variable(self.params[1])
        self.Ui = tf.Variable(self.params[2])
        self.bi = tf.Variable(self.params[3])

        self.Wf = tf.Variable(self.params[4])
        self.Uf = tf.Variable(self.params[5])
        self.bf = tf.Variable(self.params[6])

        self.Wog = tf.Variable(self.params[7])
        self.Uog = tf.Variable(self.params[8])
        self.bog = tf.Variable(self.params[9])

        self.Wc = tf.Variable(self.params[10])
        self.Uc = tf.Variable(self.params[11])
        self.bc = tf.Variable(self.params[12])
        params.extend([
            self.Wi, self.Ui, self.bi,
            self.Wf, self.Uf, self.bf,
            self.Wog, self.Uog, self.bog,
            self.Wc, self.Uc, self.bc])

        def unit(x, hidden_memory_tm1):
            previous_hidden_state, c_prev = tf.unstack(hidden_memory_tm1)

            # Input Gate
            i = tf.sigmoid(
                tf.matmul(x, self.Wi) +
                tf.matmul(previous_hidden_state, self.Ui) + self.bi
            )

            # Forget Gate
            f = tf.sigmoid(
                tf.matmul(x, self.Wf) +
                tf.matmul(previous_hidden_state, self.Uf) + self.bf
            )

            # Output Gate
            o = tf.sigmoid(
                tf.matmul(x, self.Wog) +
                tf.matmul(previous_hidden_state, self.Uog) + self.bog
            )

            # New Memory Cell
            c_ = tf.nn.tanh(
                tf.matmul(x, self.Wc) +
                tf.matmul(previous_hidden_state, self.Uc) + self.bc
            )

            # Final Memory cell
            c = f * c_prev + i * c_

            # Current Hidden state
            current_hidden_state = o * tf.nn.tanh(c)

            return tf.stack([current_hidden_state, c])

        return unit

    def create_output_unit(self, params):
        self.Wo = tf.Variable(self.params[13])
        self.bo = tf.Variable(self.params[14])
        params.extend([self.Wo, self.bo])

        def unit(hidden_memory_tuple):
            hidden_state, c_prev = tf.unstack(hidden_memory_tuple)
            # hidden_state : batch x hidden_dim
            logits = tf.matmul(hidden_state, self.Wo) + self.bo
            # output = tf.nn.softmax(logits)
            return logits

        return unit
