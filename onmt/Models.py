from __future__ import division
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
from torch.nn.utils.rnn import pack_padded_sequence as pack
from torch.nn.utils.rnn import pad_packed_sequence as unpack

import onmt
from onmt.Utils import aeq, sequence_mask


def rnn_factory(rnn_type, **kwargs):
    # Use pytorch version when available.
    no_pack_padded_seq = False
    if rnn_type == "SRU":
        # SRU doesn't support PackedSequence.
        no_pack_padded_seq = True
        rnn = onmt.modules.SRU(**kwargs)
    else:
        rnn = getattr(nn, rnn_type)(**kwargs)
    return rnn, no_pack_padded_seq


class EncoderBase(nn.Module):
    """
    Base encoder class. Specifies the interface used by different encoder types
    and required by :obj:`onmt.Models.NMTModel`.

    .. mermaid::

       graph BT
          A[Input]
          subgraph RNN
            C[Pos 1]
            D[Pos 2]
            E[Pos N]
          end
          F[Memory_Bank]
          G[Final]
          A-->C
          A-->D
          A-->E
          C-->F
          D-->F
          E-->F
          E-->G
    """

    def _check_args(self, input, lengths=None, hidden=None):
        s_len, n_batch, n_feats = input.size()
        if lengths is not None:
            n_batch_, = lengths.size()
            aeq(n_batch, n_batch_)

    def forward(self, src, lengths=None, encoder_state=None):
        """
        Args:
            src (:obj:`LongTensor`):
               padded sequences of sparse indices `[src_len x batch x nfeat]`
            lengths (:obj:`LongTensor`): length of each sequence `[batch]`
            encoder_state (rnn-class specific):
               initial encoder_state state.

        Returns:
            (tuple of :obj:`FloatTensor`, :obj:`FloatTensor`):
                * final encoder state, used to initialize decoder
                * memory bank for attention, `[src_len x batch x hidden]`
        """
        raise NotImplementedError


class MeanEncoder(EncoderBase):
    """A trivial non-recurrent encoder. Simply applies mean pooling.

    Args:
       num_layers (int): number of replicated layers
       embeddings (:obj:`onmt.modules.Embeddings`): embedding module to use
    """

    def __init__(self, num_layers, embeddings):
        super(MeanEncoder, self).__init__()
        self.num_layers = num_layers
        self.embeddings = embeddings

    def forward(self, src, lengths=None, encoder_state=None):
        "See :obj:`EncoderBase.forward()`"
        self._check_args(src, lengths, encoder_state)

        emb = self.embeddings(src)
        s_len, batch, emb_dim = emb.size()
        mean = emb.mean(0).expand(self.num_layers, batch, emb_dim)
        memory_bank = emb
        encoder_final = (mean, mean)
        return encoder_final, memory_bank


class RNNEncoder(EncoderBase):
    """ A generic recurrent neural network encoder.

    Args:
       rnn_type (:obj:`str`):
          style of recurrent unit to use, one of [RNN, LSTM, GRU, SRU]
       bidirectional (bool) : use a bidirectional RNN
       num_layers (int) : number of stacked layers
       hidden_size (int) : hidden size of each layer
       dropout (float) : dropout value for :obj:`nn.Dropout`
       embeddings (:obj:`onmt.modules.Embeddings`): embedding module to use
    """

    def __init__(self, rnn_type, bidirectional, num_layers,
                 hidden_size, dropout=0.0, embeddings=None,
                 use_bridge=False):
        super(RNNEncoder, self).__init__()
        assert embeddings is not None

        num_directions = 2 if bidirectional else 1
        assert hidden_size % num_directions == 0
        hidden_size = hidden_size // num_directions
        self.embeddings = embeddings
        self.num_layers = num_layers

        self.rnn, self.no_pack_padded_seq = \
            rnn_factory(rnn_type,
                        input_size=embeddings.embedding_size,
                        hidden_size=hidden_size,
                        num_layers=num_layers,
                        dropout=dropout,
                        bidirectional=bidirectional)

        # Initialize the bridge layer
        self.use_bridge = use_bridge
        if self.use_bridge:
            self._initialize_bridge(rnn_type,
                                    hidden_size,
                                    num_layers)

    def forward(self, src, lengths=None, encoder_state=None):
        "See :obj:`EncoderBase.forward()`"
        self._check_args(src, lengths, encoder_state)

        emb = self.embeddings(src)
        s_len, batch, emb_dim = emb.size()

        packed_emb = emb
        if lengths is not None and not self.no_pack_padded_seq:
            # Lengths data is wrapped inside a Tensor.
            lengths = lengths.view(-1).tolist()
            packed_emb = pack(emb, lengths)

        memory_bank, encoder_final = self.rnn(packed_emb, encoder_state)

        if lengths is not None and not self.no_pack_padded_seq:
            memory_bank = unpack(memory_bank)[0]

        if self.use_bridge:
            encoder_final = self._bridge(encoder_final)
        return encoder_final, memory_bank

    def _initialize_bridge(self, rnn_type,
                           hidden_size,
                           num_layers):

        # LSTM has hidden and cell state, other only one
        number_of_states = 2 if rnn_type == "LSTM" else 1
        # Total number of states
        self.total_hidden_dim = hidden_size * num_layers

        # Build a linear layer for each
        self.bridge = nn.ModuleList([nn.Linear(self.total_hidden_dim,
                                               hidden_size,  # yue suppose decoder layer # is 1
                                               bias=True)
                                     for i in range(number_of_states)])

    def _bridge(self, hidden):
        """
        Forward hidden state through bridge
        """

        def bottle_hidden(linear, states):
            """
            Transform from 3D to 2D, apply linear and return initial size
            """
            size = states.size()
            result = linear(states.view(-1, self.total_hidden_dim))
            return F.relu(result).view([size[0] / self.num_layers, size[1], size[2]])  # yue

        if isinstance(hidden, tuple):  # LSTM
            outs = tuple([bottle_hidden(layer, hidden[ix])
                          for ix, layer in enumerate(self.bridge)])
        else:
            outs = bottle_hidden(self.bridge[0], hidden)
        return outs


class BiAttEncoder(EncoderBase):
    def __init__(self, rnn_type, bidirectional, num_layers, hidden_size,
                 dropout, embeddings):
        super(BiAttEncoder, self).__init__()

        assert bidirectional
        assert num_layers == 2
        self.rnn_type = rnn_type
        hidden_size = hidden_size // 2
        self.real_hidden_size = hidden_size
        self.no_pack_padded_seq = False
        self.bidirectional = bidirectional

        self.embeddings = embeddings
        input_size_list = [embeddings.embedding_size] + [4 * hidden_size] * (num_layers - 2)
        self.src_rnn_list = nn.ModuleList([getattr(nn, rnn_type)(  # getattr(nn, rnn_type) = torch.nn.modules.rnn.LSTM
            input_size=input_sizei,
            hidden_size=hidden_size,
            num_layers=1,
            bidirectional=self.bidirectional) for input_sizei in input_size_list])
        self.merge_rnn = getattr(nn, rnn_type)(  # getattr(nn, rnn_type) = torch.nn.modules.rnn.LSTM
            input_size=4 * hidden_size,
            hidden_size=hidden_size,
            num_layers=1,
            bidirectional=self.bidirectional)
        self.combine_output = nn.Linear(4 * hidden_size, 2 * hidden_size, bias=True)
        self.combine_hidden = nn.Linear(2 * hidden_size, hidden_size, bias=True)
        self.dropout = nn.Dropout(p=dropout)

    def forward(self, src_input, ans_input, lengths=None, hidden=None):
        """
        a reading comprehension style framework for encoder
        (src->BiRNN->src_outputs, ans->BiRNN->ans_outputs)->(match->src_attn_output, ans_attn_output)
        -> (src_outputs+src_attn_output, ans_outputs+ans_attn_output)->Decoder
        src_input: post
        ans_input: conversation
        lengths: tuple (src_lengths, ans_lengths)
        """

        assert isinstance(lengths, tuple)
        src_lengths, ans_lengths = lengths

        self._check_args(src_input, src_lengths, hidden)
        self._check_args(ans_input, ans_lengths, hidden)

        src_input = self.embeddings(src_input)  # [src_seq_len, batch_size, emb_dim]
        # src_len, batch, emb_dim = src_input.size()

        ans_input = self.embeddings(ans_input)  # [ans_seq_len, batch_size, emb_dim]

        # ans_len, batch, emb_dim = ans_input.size()

        # match layer

        def ans_match(src_seq, ans_seq):
            import torch.nn.functional as F
            BF_ans_mask = sequence_mask(ans_lengths)  # [batch, ans_seq_len]
            BF_src_mask = sequence_mask(src_lengths)  # [batch, src_seq_len]
            BF_src_outputs = src_seq.transpose(0, 1)  # [batch, src_seq_len, 2*hidden_size]
            BF_ans_outputs = ans_seq.transpose(0, 1)  # [batch, ans_seq_len, 2*hidden_size]

            # compute bi-att scores
            src_scores = BF_src_outputs.bmm(BF_ans_outputs.transpose(2, 1))  # [batch, src_seq_len, ans_seq_len]
            ans_scores = BF_ans_outputs.bmm(BF_src_outputs.transpose(2, 1))  # [batch, ans_seq_len, src_seq_len]

            # mask padding
            Expand_BF_ans_mask = BF_ans_mask.unsqueeze(1).expand(src_scores.size())  # [batch, src_seq_len, ans_seq_len]
            src_scores.data.masked_fill_((1 - Expand_BF_ans_mask).byte(), -float('inf'))

            Expand_BF_src_mask = BF_src_mask.unsqueeze(1).expand(ans_scores.size())  # [batch, ans_seq_len, src_seq_len]
            ans_scores.data.masked_fill_((1 - Expand_BF_src_mask).byte(), -float('inf'))

            # normalize with softmax
            src_alpha = F.softmax(src_scores, dim=2)  # [batch, src_seq_len, ans_seq_len]
            ans_alpha = F.softmax(ans_scores, dim=2)  # [batch, ans_seq_len, src_seq_len]

            # take the weighted average
            BF_src_matched_seq = src_alpha.bmm(BF_ans_outputs)  # [batch, src_seq_len, 2*hidden_size]
            src_matched_seq = BF_src_matched_seq.transpose(0, 1)  # [src_seq_len, batch, 2*hidden_size]

            BF_ans_matched_seq = ans_alpha.bmm(BF_src_outputs)  # [batch, ans_seq_len, 2*hidden_size]
            ans_matched_seq = BF_ans_matched_seq.transpose(0, 1)  # [src_seq_len, batch, 2*hidden_size]

            return src_matched_seq, ans_matched_seq

        # sort ans w.r.t lengths
        sorted_ans_lengths, idx_sort = torch.sort(ans_lengths, dim=0, descending=True)
        _, idx_unsort = torch.sort(idx_sort, dim=0)
        idx_sort = Variable(idx_sort)  # [batch_size]
        idx_unsort = Variable(idx_unsort)  # [batch_size]

        # for src_rnn, ans_rnn in zip(self.src_rnn_list, self.ans_rnn_list):
        for layer_idx in range(len(self.src_rnn_list)):
            src_rnn = self.src_rnn_list[layer_idx]
            packed_input = src_input
            if src_lengths is not None and not self.no_pack_padded_seq:
                # Lengths data is wrapped inside a Variable.
                packed_input = pack(src_input, src_lengths.view(-1).tolist())
            # forward
            src_outputs, src_hidden = src_rnn(packed_input, hidden)
            if src_lengths is not None and not self.no_pack_padded_seq:
                # output
                src_outputs = unpack(src_outputs)[0]

            packed_ans_input = ans_input.index_select(1, idx_sort)
            if sorted_ans_lengths is not None and not self.no_pack_padded_seq:
                packed_ans_input = pack(packed_ans_input, sorted_ans_lengths.view(-1).tolist())

            ans_outputs, ans_hidden = src_rnn(packed_ans_input, hidden)
            if sorted_ans_lengths is not None and not self.no_pack_padded_seq:
                ans_outputs = unpack(ans_outputs)[0]
                ans_outputs = ans_outputs.index_select(1, idx_unsort)
                # h, c
                if self.rnn_type == 'LSTM':
                    ans_hidden = tuple([ans_hidden[i].index_select(1, idx_unsort) for i in range(2)])
                elif self.rnn_type == 'GRU':
                    ans_hidden = ans_hidden.index_select(1, idx_unsort)  # [2, batch, hidden_size]

            src_matched_seq, ans_matched_seq = ans_match(src_outputs, ans_outputs)

            src_outputs = torch.cat([src_outputs, src_matched_seq], dim=-1)  # [src_seq_len, batch_size, 4 * hidden]
            ans_outputs = torch.cat([ans_outputs, ans_matched_seq], dim=-1)  # [ans_seq_len, batch_size, 4 * hidden]

            outputs = self.combine_output(
                torch.cat([src_outputs, ans_outputs], dim=0))  # [src_seq_len+ans_seq_len, batch_size, 2 * hidden]
            hidden = self.combine_hidden(torch.cat([src_hidden, ans_hidden], dim=-1))  # [2, batch_size, hidden]

        return hidden, outputs


class RNNDecoderBase(nn.Module):
    """
    Base recurrent attention-based decoder class.
    Specifies the interface used by different decoder types
    and required by :obj:`onmt.Models.NMTModel`.


    .. mermaid::

       graph BT
          A[Input]
          subgraph RNN
             C[Pos 1]
             D[Pos 2]
             E[Pos N]
          end
          G[Decoder State]
          H[Decoder State]
          I[Outputs]
          F[Memory_Bank]
          A--emb-->C
          A--emb-->D
          A--emb-->E
          H-->C
          C-- attn --- F
          D-- attn --- F
          E-- attn --- F
          C-->I
          D-->I
          E-->I
          E-->G
          F---I

    Args:
       rnn_type (:obj:`str`):
          style of recurrent unit to use, one of [RNN, LSTM, GRU, SRU]
       bidirectional_encoder (bool) : use with a bidirectional encoder
       num_layers (int) : number of stacked layers
       hidden_size (int) : hidden size of each layer
       attn_type (str) : see :obj:`onmt.modules.GlobalAttention`
       coverage_attn (str): see :obj:`onmt.modules.GlobalAttention`
       context_gate (str): see :obj:`onmt.modules.ContextGate`
       copy_attn (bool): setup a separate copy attention mechanism
       dropout (float) : dropout value for :obj:`nn.Dropout`
       embeddings (:obj:`onmt.modules.Embeddings`): embedding module to use
    """

    def __init__(self, rnn_type, bidirectional_encoder, num_layers,
                 hidden_size, attn_type="general",
                 coverage_attn=False, context_gate=None,
                 copy_attn=False, dropout=0.0, embeddings=None,
                 reuse_copy_attn=False):
        super(RNNDecoderBase, self).__init__()

        # Basic attributes.
        self.decoder_type = 'rnn'
        self.bidirectional_encoder = bidirectional_encoder
        self.num_layers = num_layers
        self.hidden_size = hidden_size
        self.embeddings = embeddings
        self.dropout = nn.Dropout(dropout)

        # Build the RNN.
        self.rnn = self._build_rnn(rnn_type,
                                   input_size=self._input_size,
                                   hidden_size=hidden_size,
                                   num_layers=num_layers,
                                   dropout=dropout)

        # Set up the context gate.
        self.context_gate = None
        if context_gate is not None:
            self.context_gate = onmt.modules.context_gate_factory(
                context_gate, self._input_size,
                hidden_size, hidden_size, hidden_size
            )

        # Set up the standard attention.
        self._coverage = coverage_attn
        self.attn = onmt.modules.GlobalAttention(
            hidden_size, coverage=coverage_attn,
            attn_type=attn_type
        )

        # Set up a separated copy attention layer, if needed.
        self._copy = False
        if copy_attn and not reuse_copy_attn:
            self.copy_attn = onmt.modules.GlobalAttention(
                hidden_size, attn_type=attn_type
            )
        if copy_attn:
            self._copy = True
        self._reuse_copy_attn = reuse_copy_attn

    def forward(self, tgt, memory_bank, state, memory_lengths=None):
        """
        Args:
            tgt (`LongTensor`): sequences of padded tokens
                                `[tgt_len x batch x nfeats]`.
            memory_bank (`FloatTensor`): vectors from the encoder
                 `[src_len x batch x hidden]`.
            state (:obj:`onmt.Models.DecoderState`):
                 decoder state object to initialize the decoder
            memory_lengths (`LongTensor`): the padded source lengths
                `[batch]`.
        Returns:
            (`FloatTensor`,:obj:`onmt.Models.DecoderState`,`FloatTensor`):
                * decoder_outputs: output from the decoder (after attn)
                         `[tgt_len x batch x hidden]`.
                * decoder_state: final hidden state from the decoder
                * attns: distribution over src at each tgt
                        `[tgt_len x batch x src_len]`.
        """
        # Check
        assert isinstance(state, RNNDecoderState)
        tgt_len, tgt_batch, _ = tgt.size()
        _, memory_batch, _ = memory_bank.size()
        aeq(tgt_batch, memory_batch)
        # END

        # Run the forward pass of the RNN.
        decoder_final, decoder_outputs, attns = self._run_forward_pass(
            tgt, memory_bank, state, memory_lengths=memory_lengths)

        # Update the state with the result.
        final_output = decoder_outputs[-1]
        coverage = None
        if "coverage" in attns:
            coverage = attns["coverage"][-1].unsqueeze(0)
        state.update_state(decoder_final, final_output.unsqueeze(0), coverage)

        # Concatenates sequence of tensors along a new dimension.
        if not isinstance(decoder_outputs, torch.Tensor):
            decoder_outputs = torch.stack(decoder_outputs)
        for k in attns:
            if not isinstance(attns[k], torch.Tensor):
                attns[k] = torch.stack(attns[k])

        return decoder_outputs, state, attns

    def init_decoder_state(self, src, memory_bank, encoder_final):

        def _fix_enc_hidden(h):
            # The encoder hidden is  (layers*directions) x batch x dim.
            # We need to convert it to layers x batch x (directions*dim).
            if self.bidirectional_encoder:
                h = torch.cat([h[0:h.size(0):2], h[1:h.size(0):2]], 2)
            return h

        if isinstance(encoder_final, tuple):  # LSTM
            return RNNDecoderState(self.hidden_size,
                                   tuple([_fix_enc_hidden(enc_hid)
                                          for enc_hid in encoder_final]))
        else:  # GRU
            return RNNDecoderState(self.hidden_size,
                                   _fix_enc_hidden(encoder_final))


class StdRNNDecoder(RNNDecoderBase):
    """
    Standard fully batched RNN decoder with attention.
    Faster implementation, uses CuDNN for implementation.
    See :obj:`RNNDecoderBase` for options.


    Based around the approach from
    "Neural Machine Translation By Jointly Learning To Align and Translate"
    :cite:`Bahdanau2015`


    Implemented without input_feeding and currently with no `coverage_attn`
    or `copy_attn` support.
    """

    def _run_forward_pass(self, tgt, memory_bank, state, memory_lengths=None):
        """
        Private helper for running the specific RNN forward pass.
        Must be overriden by all subclasses.
        Args:
            tgt (LongTensor): a sequence of input tokens tensors
                                 [len x batch x nfeats].
            memory_bank (FloatTensor): output(tensor sequence) from the encoder
                        RNN of size (src_len x batch x hidden_size).
            state (FloatTensor): hidden state from the encoder RNN for
                                 initializing the decoder.
            memory_lengths (LongTensor): the source memory_bank lengths.
        Returns:
            decoder_final (Tensor): final hidden state from the decoder.
            decoder_outputs ([FloatTensor]): an array of output of every time
                                     step from the decoder.
            attns (dict of (str, [FloatTensor]): a dictionary of different
                            type of attention Tensor array of every time
                            step from the decoder.
        """
        assert not self._copy  # TODO, no support yet.
        assert not self._coverage  # TODO, no support yet.

        # Initialize local and return variables.
        attns = {}
        emb = self.embeddings(tgt)

        # Run the forward pass of the RNN.
        if isinstance(self.rnn, nn.GRU):
            rnn_output, decoder_final = self.rnn(emb, state.hidden[0])
        else:
            rnn_output, decoder_final = self.rnn(emb, state.hidden)

        # Check
        tgt_len, tgt_batch, _ = tgt.size()
        output_len, output_batch, _ = rnn_output.size()
        aeq(tgt_len, output_len)
        aeq(tgt_batch, output_batch)
        # END

        # Calculate the attention.
        decoder_outputs, p_attn = self.attn(
            rnn_output.transpose(0, 1).contiguous(),
            memory_bank.transpose(0, 1),
            memory_lengths=memory_lengths
        )
        attns["std"] = p_attn

        # Calculate the context gate.
        if self.context_gate is not None:
            decoder_outputs = self.context_gate(
                emb.view(-1, emb.size(2)),
                rnn_output.view(-1, rnn_output.size(2)),
                decoder_outputs.view(-1, decoder_outputs.size(2))
            )
            decoder_outputs = \
                decoder_outputs.view(tgt_len, tgt_batch, self.hidden_size)

        decoder_outputs = self.dropout(decoder_outputs)
        return decoder_final, decoder_outputs, attns

    def _build_rnn(self, rnn_type, **kwargs):
        rnn, _ = rnn_factory(rnn_type, **kwargs)
        return rnn

    @property
    def _input_size(self):
        """
        Private helper returning the number of expected features.
        """
        return self.embeddings.embedding_size


class InputFeedRNNDecoder(RNNDecoderBase):
    """
    Input feeding based decoder. See :obj:`RNNDecoderBase` for options.

    Based around the input feeding approach from
    "Effective Approaches to Attention-based Neural Machine Translation"
    :cite:`Luong2015`


    .. mermaid::

       graph BT
          A[Input n-1]
          AB[Input n]
          subgraph RNN
            E[Pos n-1]
            F[Pos n]
            E --> F
          end
          G[Encoder]
          H[Memory_Bank n-1]
          A --> E
          AB --> F
          E --> H
          G --> H
    """

    def _run_forward_pass(self, tgt, memory_bank, state, memory_lengths=None):
        """
        See StdRNNDecoder._run_forward_pass() for description
        of arguments and return values.
        """
        # Additional args check.
        input_feed = state.input_feed.squeeze(0)
        input_feed_batch, _ = input_feed.size()
        tgt_len, tgt_batch, _ = tgt.size()
        aeq(tgt_batch, input_feed_batch)
        # END Additional args check.

        # Initialize local and return variables.
        decoder_outputs = []
        attns = {"std": []}
        if self._copy:
            attns["copy"] = []
        if self._coverage:
            attns["coverage"] = []

        emb = self.embeddings(tgt)
        assert emb.dim() == 3  # len x batch x embedding_dim

        hidden = state.hidden
        coverage = state.coverage.squeeze(0) \
            if state.coverage is not None else None

        # Input feed concatenates hidden state with
        # input at every time step.
        for i, emb_t in enumerate(emb.split(1)):
            emb_t = emb_t.squeeze(0)
            decoder_input = torch.cat([emb_t, input_feed], 1)

            rnn_output, hidden = self.rnn(decoder_input, hidden)
            decoder_output, p_attn = self.attn(
                rnn_output,
                memory_bank.transpose(0, 1),
                memory_lengths=memory_lengths,
                max_src_len=None)
            if self.context_gate is not None:
                # TODO: context gate should be employed
                # instead of second RNN transform.
                decoder_output = self.context_gate(
                    decoder_input, rnn_output, decoder_output
                )
            decoder_output = self.dropout(decoder_output)
            input_feed = decoder_output

            decoder_outputs += [decoder_output]
            attns["std"] += [p_attn]

            # Update the coverage attention.
            if self._coverage:
                coverage = coverage + p_attn \
                    if coverage is not None else p_attn
                attns["coverage"] += [coverage]

            # Run the forward pass of the copy attention layer.
            if self._copy and not self._reuse_copy_attn:
                _, copy_attn = self.copy_attn(decoder_output,
                                              memory_bank.transpose(0, 1))
                attns["copy"] += [copy_attn]
            elif self._copy:
                attns["copy"] = attns["std"]
        # Return result.
        return hidden, decoder_outputs, attns

    def _build_rnn(self, rnn_type, input_size,
                   hidden_size, num_layers, dropout):
        assert not rnn_type == "SRU", "SRU doesn't support input feed! " \
                                      "Please set -input_feed 0!"
        if rnn_type == "LSTM":
            stacked_cell = onmt.modules.StackedLSTM
        else:
            stacked_cell = onmt.modules.StackedGRU
        return stacked_cell(num_layers, input_size,
                            hidden_size, dropout)

    @property
    def _input_size(self):
        """
        Using input feed by concatenating input with attention vectors.
        """
        return self.embeddings.embedding_size + self.hidden_size


class NMTModel(nn.Module):
    """
    Core trainable object in OpenNMT. Implements a trainable interface
    for a simple, generic encoder + decoder model.

    Args:
      encoder (:obj:`EncoderBase`): an encoder object
      decoder (:obj:`RNNDecoderBase`): a decoder object
      multi<gpu (bool): setup for multigpu support
    """

    def __init__(self, encoder, decoder, multigpu=False):
        self.multigpu = multigpu
        super(NMTModel, self).__init__()
        self.encoder = encoder
        self.decoder = decoder

    def forward(self, src, tgt, lengths, dec_state=None):
        """Forward propagate a `src` and `tgt` pair for training.
        Possible initialized with a beginning decoder state.

        Args:
            src (:obj:`Tensor`):
                a source sequence passed to encoder.
                typically for inputs this will be a padded :obj:`LongTensor`
                of size `[len x batch x features]`. however, may be an
                image or other generic input depending on encoder.
            tgt (:obj:`LongTensor`):
                 a target sequence of size `[tgt_len x batch]`.
            lengths(:obj:`LongTensor`): the src lengths, pre-padding `[batch]`.
            dec_state (:obj:`DecoderState`, optional): initial decoder state
        Returns:
            (:obj:`FloatTensor`, `dict`, :obj:`onmt.Models.DecoderState`):

                 * decoder output `[tgt_len x batch x hidden]`
                 * dictionary attention dists of `[tgt_len x batch x src_len]`
                 * final decoder state
        """
        tgt = tgt[:-1]  # exclude last target from inputs

        enc_final, memory_bank = self.encoder(src, lengths)
        enc_state = \
            self.decoder.init_decoder_state(src, memory_bank, enc_final)
        decoder_outputs, dec_state, attns = \
            self.decoder(tgt, memory_bank,
                         enc_state if dec_state is None
                         else dec_state,
                         memory_lengths=lengths)
        if self.multigpu:
            # Not yet supported on multi-gpu
            dec_state = None
            attns = None
        return decoder_outputs, attns, dec_state

class word_memory(nn.Module):

    def __init__(self, embedding_b, all_docs):
        super(word_memory, self).__init__()
        self.word_embedding_a = nn.Parameter(all_docs.permute(1,0))
        self.word_embedding_b = embedding_b
        self.word_embedding_c = nn.Parameter(all_docs.clone())

    def forward(self, word_seq, all_docs):
        '''
            word_seq: [300,13,1] [seq_len, batch_size, 1]
            all_docs: [300,1100,1] [seq_len, all_docs_num, 1]
            all_docs_emb: [550000,768]
            embedding word_seq by sen_transformer   [300,13,1] - > [13,768]
        '''

        usetopk = False  # use only topk similar documents
        u = torch.mean(self.word_embedding_b(word_seq),0)
        p = F.softmax(torch.matmul(u, self.word_embedding_a), dim=1)
        if usetopk:
            #only use topk similar documents
            prob, idx = p.topk(5, dim=1)
            m = m.permute(1, 0)
            topkm = m[idx]
            topkc = c[idx]
            u = u.unsqueeze(2)
            topkp = F.softmax(torch.matmul(topkm, u), dim=1).permute(0, 2, 1)  # [13, 1, 5]
            o = torch.matmul(topkp, topkc).squeeze(1)
        else:
            #use all 550000 documents
            o = torch.matmul(p, self.word_embedding_c)


        return o

class TwoEncoderModel(nn.Module):
    """
    Different from NMTModel, the TwoEncoderModel has two encoders for the post and conversation
    """

    def __init__(self, encoder, decoder, all_docs, embedding_b, multigpu=False):
        self.multigpu = multigpu
        super(TwoEncoderModel, self).__init__()
        self.encoder = encoder
        self.decoder = decoder
        self.all_docs = all_docs
        self.cal_word_memory = word_memory(embedding_b, all_docs)

    def forward(self, src, conversation, tgt, lengths, max_src_len=None, max_conv_len=None, dec_state=None):
        """Forward propagate a `src` `conversation` and `tgt` pair for training.
        Possible initialized with a beginning decoder state.
        src: [src_len, batch_size, dim]
        """
        tgt = tgt[:-1]  # exclude last target from inputs

        # print("LENGTHS LEN: ", len(lengths))
        # print(lengths[0].shape)
        # print(lengths[1].shape)
        ### temporarily remove conv
        ### temporarily concatenate
        concat = torch.cat((src, conversation), 0)
        concat_lengths = lengths[0] + lengths[1]

        enc_final, memory_bank = self.encoder(concat, concat_lengths)

        if self.all_docs is not None:
            o = self.cal_word_memory(src, self.all_docs)
            print(o.shape, memory_bank.shape)
            memory_bank = torch.add(memory_bank, o)

        enc_state = \
            self.decoder.init_decoder_state(concat, memory_bank, enc_final)

        decoder_outputs, dec_state, attns = \
            self.decoder(tgt, memory_bank,
                         enc_state if dec_state is None
                         else dec_state,
                         memory_lengths=concat_lengths)
        if self.multigpu:
            # Not yet supported on multi-gpu
            dec_state = None
            attns = None
        return decoder_outputs, attns, dec_state


class DecoderState(object):
    """Interface for grouping together the current state of a recurrent
    decoder. In the simplest case just represents the hidden state of
    the model.  But can also be used for implementing various forms of
    input_feeding and non-recurrent models.

    Modules need to implement this to utilize beam search decoding.
    """

    def detach(self):
        for h in self._all:
            if h is not None:
                h.detach_()

    def beam_update(self, idx, positions, beam_size):
        for e in self._all:
            sizes = e.size()
            br = sizes[1]
            if len(sizes) == 3:
                sent_states = e.view(sizes[0], beam_size, br // beam_size,
                                     sizes[2])[:, :, idx]
            else:
                sent_states = e.view(sizes[0], beam_size,
                                     br // beam_size,
                                     sizes[2],
                                     sizes[3])[:, :, idx]

            sent_states.data.copy_(
                sent_states.data.index_select(1, positions))


class RNNDecoderState(DecoderState):
    def __init__(self, hidden_size, rnnstate):
        """
        Args:
            hidden_size (int): the size of hidden layer of the decoder.
            rnnstate: final hidden state from the encoder.
                transformed to shape: layers x batch x (directions*dim).
        """
        if not isinstance(rnnstate, tuple):
            self.hidden = (rnnstate,)
        else:
            self.hidden = rnnstate
        self.coverage = None

        # Init the input feed.
        batch_size = self.hidden[0].size(1)
        h_size = (batch_size, hidden_size)
        self.input_feed = self.hidden[0].data.new(*h_size).zero_() \
            .unsqueeze(0)

    @property
    def _all(self):
        return self.hidden + (self.input_feed,)

    def update_state(self, rnnstate, input_feed, coverage):
        if not isinstance(rnnstate, tuple):
            self.hidden = (rnnstate,)
        else:
            self.hidden = rnnstate
        self.input_feed = input_feed
        self.coverage = coverage

    def repeat_beam_size_times(self, beam_size):
        """ Repeat beam_size times along batch dimension. """
        vars = [e.data.repeat(1, beam_size, 1)
                for e in self._all]
        self.hidden = tuple(vars[:-1])
        self.input_feed = vars[-1]
