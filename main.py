# coding: utf-8
import argparse
import time
import math
import torch
import torch.nn as nn
from torch.autograd import Variable

import torch.optim as optim
import rnn_attention
import data
from adabound import AdaBound
# import model


parser = argparse.ArgumentParser(description='PTB RNN/LSTM Language Model: Main Function')
parser.add_argument('--data', type=str, default='./data/ptb',
                    help='location of the data corpus')
parser.add_argument('--model', type=str, default='LSTM',
                    help='type of recurrent net (RNN_TANH, RNN_RELU, LSTM, GRU)')
parser.add_argument('--emsize', type=int, default=100,
                    help='size of word embeddings')
parser.add_argument('--nhid', type=int, default=128,
                    help='number of hidden units per layer')
parser.add_argument('--nlayers', type=int, default=1,
                    help='number of layers')
parser.add_argument('--lr', type=float, default=20,
                    help='initial learning rate')
# parser.add_argument('--clip', type=float, default=0.25,
#                     help='gradient clipping')
parser.add_argument('--epochs', type=int, default=200,
                    help='upper epoch limit')
parser.add_argument('--batch_size', type=int, default=32, metavar='N',
                    help='batch size')
parser.add_argument('--bptt', type=int, default=35,
                    help='sequence length')
parser.add_argument('--dropout', type=float, default=0.2,
                    help='dropout applied to layers (0 = no dropout)')
parser.add_argument('--tied', action='store_true',
                    help='tie the word embedding and softmax weights')
parser.add_argument('--seed', type=int, default=1111,
                    help='random seed')
parser.add_argument('--log_interval', type=int, default=200, metavar='N',
                    help='report interval')
parser.add_argument('--save', type=str,  default='model.pt',
                    help='path to save the final model')
parser.add_argument('--optim', default='sgd', type=str, help='optimizer',
                    choices=['sgd', 'adagrad', 'adam', 'amsgrad', 'adabound', 'amsbound'])
parser.add_argument('--momentum', default=0.9, type=float, help='momentum term')
parser.add_argument('--beta1', default=0.9, type=float, help='Adam coefficients beta_1')
parser.add_argument('--beta2', default=0.999, type=float, help='Adam coefficients beta_2')
parser.add_argument('--final_lr', default=0.1, type=float,
                    help='final learning rate of AdaBound')
parser.add_argument('--gamma', default=1e-3, type=float,)
parser.add_argument('--ita',default=1e-2, type=float)
parser.add_argument('--weight_decay', type=float, default=5e-4,
                    help='weight decay applied to all weights')
#
# parser.add_argument('--att', action='store_true',
#                     help='attention layers')
#
# parser.add_argument('--att_width', type=int,  default=3,
#                     help='attention layer width')

parser.add_argument('--cuda', action='store_true',
                    help='use CUDA')
args = parser.parse_args()

# check attention width and sequence length
# try:
#     args.att_width < args.bptt
# except KeyError:
#
#     raise ValueError("""attention width should be less than sequence length,
#                         att_width < bptt""")

if torch.cuda.is_available():
    if not args.cuda:
        print("WARNING: You have a CUDA device, so you should probably run with --cuda")
    else:
        torch.cuda.manual_seed(args.seed)
# Set the random seed manually for reproducibility.
torch.manual_seed(args.seed)

###############################################################################
# Load data
###############################################################################

corpus = data.Corpus(args.data)

# Starting from sequential data, batchify arranges the dataset into columns.
# For instance, with the alphabet as the sequence and batch size 4, we'd get
# ┌ a g m s ┐
# │ b h n t │
# │ c i o u │
# │ d j p v │
# │ e k q w │
# └ f l r x ┘.
# These columns are treated as independent by the model, which means that the
# dependence of e. g. 'g' on 'f' can not be learned, but allows more efficient
# batch processing.

def batchify(data, bsz):
    # Work out how cleanly we can divide the dataset into bsz parts.
    nbatch = data.size(0) // bsz
    # Trim off any extra elements that wouldn't cleanly fit (remainders).
    data = data.narrow(0, 0, nbatch * bsz)#这一步和上一步的效果是将data归整，去掉不是整batch的数据
    # Evenly divide the data across the bsz batches.
    data = data.view(bsz, -1).t().contiguous()
    if args.cuda:
        data = data.cuda()
    return data

eval_batch_size = 200
train_data = batchify(corpus.train, args.batch_size)
val_data = batchify(corpus.valid, eval_batch_size)
test_data = batchify(corpus.test, eval_batch_size)

###############################################################################
# Build the model
###############################################################################

ntokens = len(corpus.dictionary)
#model = model.RNNModel(args.model, ntokens, args.emsize, args.nhid, args.nlayers, args.dropout,
#                               args.tied)
model = rnn_attention.RNNModel(args.model, ntokens, args.emsize, args.nhid, args.nlayers, args.dropout,
                               args.tied,  args.cuda)
#                              args.att, args.att_width,
criterion = nn.CrossEntropyLoss()
if args.cuda:
    model.cuda()

###############################################################################
# Training code
###############################################################################

# def repackage_hidden(h):
#     """Wraps hidden states in new Variables, to detach them from their history."""
#     if type(h) == Variable:
#         return Variable(h.data)
#     else:
#         return tuple(repackage_hidden(v) for v in h)

def repackage_hidden(h):
    """Wraps hidden states in new Tensors,
    to detach them from their history."""
    if isinstance(h, torch.Tensor):
        return h.detach()
    else:
        return tuple(repackage_hidden(v) for v in h)

# get_batch subdivides the source data into chunks of length args.bptt.
# If source is equal to the example output of the batchify function, with
# a bptt-limit of 2, we'd get the following two Variables for i = 0:
# ┌ a g m s ┐ ┌ b h n t ┐
# └ b h n t ┘ └ c i o u ┘
# Note that despite the name of the function, the subdivison of data is not
# done along the batch dimension (i.e. dimension 1), since that was handled
# by the batchify function. The chunks are along dimension 0, corresponding
# to the seq_len dimension in the LSTM.

def get_batch(source, i, evaluation=False):
    seq_len = min(args.bptt, len(source) - 1 - i)
    # data = Variable(source[i:i+seq_len], volatile=evaluation)
    # target = Variable(source[i+1:i+1+seq_len].view(-1))
    data = source[i:i + seq_len]
    target = source[i + 1:i + 1 + seq_len].view(-1)
    return data, target


def evaluate(data_source):
    # Turn on evaluation mode which disables dropout.
    model.eval()
    total_loss = 0
    ntokens = len(corpus.dictionary)
    hidden = model.init_hidden(eval_batch_size)
    for i in range(0, data_source.size(0) - 1, args.bptt):
        data, targets = get_batch(data_source, i, evaluation=True)
        output, hidden = model(data, hidden)
        output_flat = output.view(-1, ntokens)
        total_loss += len(data) * criterion(output_flat, targets).data
        hidden = repackage_hidden(hidden)
    return total_loss / len(data_source)


def train():
    # Turn on training mode which enables dropout.
    model.train()
    total_loss = 0
    # start_time = time.time()
    ntokens = len(corpus.dictionary)
    hidden = model.init_hidden(args.batch_size)
    for batch, i in enumerate(range(0, train_data.size(0) - 1, args.bptt)):
        data, targets = get_batch(train_data, i)
        # Starting each batch, we detach the hidden state from how it was previously produced.
        # If we didn't, the model would try backpropagating all the way to start of the dataset.
        hidden = repackage_hidden(hidden)
        model.zero_grad()
        output, hidden = model(data, hidden)
        #print(output.size())
        loss = criterion(output.view(-1, ntokens), targets)
        loss.backward()

        # `clip_grad_norm` helps prevent the exploding gradient problem in RNNs / LSTMs.
        # torch.nn.utils.clip_grad_norm(model.parameters(), args.clip)
        #print(model.state_dict())
        #print("Done***")

        # for n,p in model.named_parameters():
        #     p.data.add_(-lr, p.grad.data)

        optimizer.step()
        total_loss += loss.data

        # if batch % args.log_interval == 0 and batch > 0:
        #     cur_loss = total_loss.item() / args.log_interval
            # elapsed = time.time() - start_time
    train_loss = total_loss / batch
    print('train_loss {:5.2f} | perplexity {:8.2f}'.format(train_loss, math.exp(train_loss)))
    total_loss = 0
            # start_time = time.time()

# Loop over epochs.
lr = args.lr
best_val_loss = None

#optimizer
def create_optimizer(args, model_params):
    if args.optim == 'sgd':
        return optim.SGD(model_params, args.lr, momentum=args.momentum,
                         weight_decay=args.weight_decay)
    elif args.optim == 'adagrad':
        return optim.Adagrad(model_params, args.lr, weight_decay=args.weight_decay)
    elif args.optim == 'adam':
        return optim.Adam(model_params, args.lr, betas=(args.beta1, args.beta2),
                          weight_decay=args.weight_decay)
    elif args.optim == 'amsgrad':
        return optim.Adam(model_params, args.lr, betas=(args.beta1, args.beta2),
                          weight_decay=args.weight_decay, amsgrad=True)
    elif args.optim == 'adabound':
        return AdaBound(model_params, args.lr, betas=(args.beta1, args.beta2),
                        final_lr=args.final_lr, gamma=args.gamma,
                        weight_decay=args.weight_decay)
    else:
        assert args.optim == 'amsbound'
        return AdaBound(model_params, args.lr, betas=(args.beta1, args.beta2),
                        final_lr=args.final_lr, gamma=args.gamma,
                        weight_decay=args.weight_decay, amsbound=True)

optimizer = create_optimizer(args, model.parameters())


# At any point you can hit Ctrl + C to break out of training early.
try:
    for epoch in range(1, args.epochs+1):
        # epoch_start_time = time.time()
        print("epoch:" + str(epoch))
        train()
        test_loss = evaluate(test_data)
        print('test loss {:5.2f} | test perplexity {:8.2f}'.format(test_loss, math.exp(test_loss)))
        val_loss = evaluate(val_data)
        print('valid loss {:5.2f} | valid perplexity {:8.2f}'.format( val_loss, math.exp(val_loss)))
        # Save the model if the validation loss is the best we've seen so far.
        if not best_val_loss or val_loss < best_val_loss:
            with open(args.save, 'wb') as f:
                torch.save(model, f)
            best_val_loss = val_loss
        else:
            # Anneal the learning rate if no improvement has been seen in the validation dataset.
            lr /= 4.0
        lr /= 2.0
except KeyboardInterrupt:
    print('-' * 89)
    print('Exiting from training early')

# Load the best saved model.
# with open(args.save, 'rb') as f:
#     model = torch.load(f)
