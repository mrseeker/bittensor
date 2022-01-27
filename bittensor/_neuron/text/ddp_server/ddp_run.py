#!/bin/python3
# The MIT License (MIT)
# Copyright © 2021 Yuma Rao

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.
""" Advanced server neuron.

Example:
    $ python miners/text/ddp_server/main.py

"""
from re import I
import bittensor
import torch
import wandb
import pandas
import datetime
import traceback
import sys
import os

from loguru import logger; logger = logger.opt(colors=True)
from torch.nn.utils import clip_grad_norm_
from datetime import datetime,timedelta
from threading import Lock
from torch.nn.parallel import DistributedDataParallel as DDP
import torch.distributed as dist
import torch.multiprocessing as mp
import time
import queue
from multiprocessing import Process, Queue
import threading

os.environ['TOKENIZERS_PARALLELISM'] = 'false'

torch.autograd.set_detect_anomaly(True) 

class ProducerThread(threading.Thread):
    r""" This producer thread runs in backgraound to fill the queue with the result of the target function.
    """
    def __init__(self, queue, target, name=None):
        r"""Initialization.
        Args:
            queue (:obj:`queue.Queue`, `required`)
                The queue to be filled.
                
            target (:obj:`function`, `required`)
                The target function to run when the queue is not full.

            arg (:type:`tuple`, `required`)
                The arguments to be passed to the target function.

            name (:type:`str`, `optional`)
                The name of this threading object. 
        """
        super(ProducerThread,self).__init__()
        self.name = name
        self.target = target
        # self.arg = arg
        self.queue = queue 
        self._stop_event = threading.Event()

    def run(self):
        r""" Work of the thread. Keep checking if the queue is full, if it is not full, run the target function to fill the queue.
        """
        while True and (not self.stopped()):
            if not self.queue.full():
                item = self.target()
                self.queue.put(item)
                time.sleep(10)
        return

    def stop(self):
        self._stop_event.set()

    def stopped(self):
        return self._stop_event.is_set()

class QueueResolver():
    r""" Manages the queue the producer thread that monitor and fills the queue.
    """
    def __init__(self, queue, target, buffer_size = 5):
        """ Setup the queue and start the producer thread.
        
        Args:
                
            producer_target (:obj:`function`, `required`)
                The target function to run when the queue is not full.

            producer_arg (:type:`tuple`, `required`)
                The arguments to be passed to the target function.

            buffer_size (:type:`int`, `optional`)
                The size of the queue.
        """
        self.buffer_size = buffer_size
        self.queue = queue
        self.producer = ProducerThread(name='producer', queue = self.queue, target = target)
        self.producer.start()

    def close(self):
        self.producer.stop()
        self.producer.join()
class DDPAxonPipe():
    def __init__( self, config: 'bittensor.config', gp_server, wallet: 'bittensor.wallet', forward_q, output_q):
        r""" Initializes the neuron with the passed config.
        """
        torch.autograd.set_detect_anomaly(True) 
        self.config = config
        self.config.to_defaults()
        self.gp_server = gp_server.to(gp_server.device)
        self.wallet = wallet
        self.world_size = config.neuron.world_size
        self.forward_q = forward_q
        self.output_q = output_q
        # self.stats = SimpleNamespace(
        #     global_step = 0,
        #     last_sync_block = 0,
        #     epoch_data_size = 0,
        #     epoch_sync_count = 0,
        #     local_target_epoch_loss = math.inf,
        #     distillation_epoch_loss = math.inf,
        #     remote_target_epoch_loss = math.inf,
        #     local_epoch_acc = 0,
        #     best_epoch_loss = math.inf,
        #     scores = {},
        #     ema_scores = torch.nn.Parameter(torch.zeros(self.config.nucleus.max_n), requires_grad = False)
        # )
        # ---- Decay factor for fisher ema score 
        # self.fisher_ema_decay = 0.995
        # self.mutex = Lock()

    def stop( self ):
        r""" Stop the dendrite and dataset
        """
        del self.dendrite
        # self.dataset.close()
    
    def init_process(self, rank):
        r""" For each process, anchor them to the process group 
        so that they know how to communication with each other.

        Args:
            rank (int):
                rank (id) of the process.
        """
        os.environ['MASTER_ADDR'] = self.config.neuron.address
        os.environ['MASTER_PORT'] = self.config.neuron.port
        if 'cuda' in self.config.neuron.device:
            backend = 'nccl'
        else:
            backend = 'gloo'

        dist.init_process_group(backend, rank=rank, world_size=self.world_size)
    
    def init_bit(self, rank = 0):
        r""" Init bittensor modules .
        
        Args:
            rank (int):
                rank (id) of the process.
        """

        if self.config.neuron.multiprocessing and self.config.neuron.device == 'cuda':
            self.device = torch.device( device = f'cuda:{rank}' )
        else:
            self.device = torch.device( device = self.config.neuron.device )
        
        self.subtensor = bittensor.subtensor ( config = self.config )
        self.metagraph = bittensor.metagraph ( config = self.config, subtensor = self.subtensor )
        self.metagraph.sync()
        # self.dataset = bittensor.dataset ( config = self.config)
        self.optimizer = torch.optim.SGD(
            [ {'params': self.gp_server.parameters() } ],
            lr = self.config.neuron.learning_rate,
            momentum = self.config.neuron.momentum,
        )
        
        if rank == 0 :
            self.subtensor.register( self.wallet )


    def cleanup(self):
        r""" Kill the process.
        """
        dist.destroy_process_group()

    def run_parallel( self ):
        r""" Spawn multiple processes.
        """
        mp.spawn(self.run,
            args=(self.world_size,),
            nprocs=self.world_size,
            join=True,
        )

    def run(self, rank = 0, world_size = 0):
        # load our old model
        # self.init_process(rank)
        self.init_bit(rank)
        if self.config.neuron.no_restart != True:
            self.gp_server.load(self.config.neuron.full_path)

        # self.gp_server = DDP(self.gp_server, bucket_cap_mb = 10000000)

        if rank == 0 and self.config.wandb.api_key != 'default':
            # --- Init Wandb.
            bittensor.wandb(
                config = self.config,
                cold_pubkey = self.wallet.coldkeypub.ss58_address,
                hot_pubkey = self.wallet.hotkey.ss58_address,
                root_dir = self.config.neuron.full_path
            )

        nn = self.subtensor.neuron_for_pubkey(self.wallet.hotkey.ss58_address)

        # --- last sync block 
        last_sync_block = self.subtensor.get_current_block()
        last_set_block = last_sync_block

        # -- Main Training loop --
        try:
            # data = next(self.dataset)

            # --- creating our chain weights
            chain_weights = torch.zeros(self.metagraph.n)
            uid = nn.uid
            chain_weights[uid] = 1 
            # with self.gp_server.join():
            bittensor.logging.success("axon pipe run", sufix = f'1 rank: {rank}')
            while True:
                success = False 
                while not success:
                    if not self.forward_q.empty() :
                        future_id, inputs_x = self.forward_q.get()
                        if inputs_x != None:
                            bittensor.logging.success("axon pipe got input", sufix = f'rank: {rank}')
                            output = self.gp_server.forward(inputs_x)
                            self.output_q.put((future_id, output.detach()))
                            print(output)
                            success = True
                    else:
                        time.sleep(2)
        except Exception as e:
            print(e)


        #         # --- Run 
        #         current_block = self.subtensor.get_current_block()
        #         end_block = current_block + self.config.neuron.blocks_per_epoch
        #         interation = 0

        #         # --- Training step.
        #         # with self.gp_server.join():
        #         while (end_block >= current_block):
        #             if current_block != self.subtensor.get_current_block():
        #                 logger.info(f'Forward Started Rank: {rank}, Iter: {interation}')
        #                 loss, _ = self.gp_server( next( self.dataset ).to( self.gp_server.device) )
        #                 losses = loss if interation == 0 else losses + loss
        #                 interation += 1
        #                 current_block = self.subtensor.get_current_block()

        #         #Custom learning rate
        #         # if self.gp_server.backward_gradients > 0:
        #         #     self.optimizer.param_groups[0]['lr'] =  1/(self.gp_server.backward_gradients)
        #         # else:
        #         self.optimizer.param_groups[0]['lr'] =  0.1
                
        #         # --- Update parameters
        #         # if interation != 0 or self.gp_server.backward_gradients != 0:
        #         #     with self.mutex:
        #         logger.info('Backpropagation Started')
        #         if interation != 0:
        #             losses.backward()
        #         clip_grad_norm_(self.gp_server.parameters(), 1.0)
                
        #         self.optimizer.step()
        #         self.optimizer.zero_grad()
        #         logger.info('Backpropagation Successful: Model updated')

        #         nn = self.subtensor.neuron_for_pubkey(self.wallet.hotkey.ss58_address)

        #         self.gp_server.backward_gradients = 0
        #         # --- logging data
        #         wandb_data = {
        #             'block': end_block,
        #             'loss': losses.cpu().item()/interation,
        #             'stake': nn.stake,
        #             'rank': nn.rank,
        #             'incentive': nn.incentive,
        #             'trust': nn.trust,
        #             'consensus': nn.consensus,
        #             'incentive': nn.incentive,
        #             'dividends': nn.dividends,
        #             'emission':  nn.emission,
        #         } 
        #         bittensor.__console__.print('[green]Current Status:[/green]', wandb_data)

        #         # Add additional wandb data for axon, metagraph etc.
        #         if self.config.wandb.api_key != 'default':

        #             df = pandas.concat( [
        #                 bittensor.utils.indexed_values_to_dataframe( prefix = 'w_i_{}'.format(nn.uid), index = self.metagraph.uids, values = self.metagraph.W[:, uid] ),
        #                 bittensor.utils.indexed_values_to_dataframe( prefix = 's_i'.format(nn.uid), index = self.metagraph.uids, values = self.metagraph.S ),
        #                 # axon.to_dataframe( metagraph = self.metagraph ),
        #             ], axis = 1)
        #             df['uid'] = df.index
        #             stats_data_table = wandb.Table( dataframe = df ) 
        #             # wandb_info_axon = axon.to_wandb()                
        #             wandb.log( { **wandb_data }, step = current_block )
        #             wandb.log( { 'stats': stats_data_table }, step = current_block )
        #             wandb.log( { 'axon_query_times': wandb.plot.scatter( stats_data_table, "uid", "axon_query_time", title="Axon Query time by UID") } )
        #             wandb.log( { 'in_weights': wandb.plot.scatter( stats_data_table, "uid", 'w_i_{}'.format(nn.uid), title="Inward weights by UID") } )
        #             wandb
        #         # --- Run 
        #         current_block = self.subtensor.get_current_block()
        #         end_block = current_block + self.config.neuron.blocks_per_epoch
        #         interation = 0

        #         # --- Training step.
        #         # with self.gp_server.join():
        #         while (end_block >= current_block):
        #             if current_block != self.subtensor.get_current_block():
        #                 logger.info(f'Forward Started Rank: {rank}, Iter: {interation}')
        #                 loss, _ = self.gp_server( next( self.dataset ).to( self.gp_server.device) )
        #                 losses = loss if interation == 0 else losses + loss
        #                 interation += 1
        #                 current_block = self.subtensor.get_current_block()

        #         #Custom learning rate
        #         # if self.gp_server.backward_gradients > 0:
        #         #     self.optimizer.param_groups[0]['lr'] =  1/(self.gp_server.backward_gradients)
        #         # else:
        #         self.optimizer.param_groups[0]['lr'] =  0.1
                
        #         # --- Update parameters
        #         # if interation != 0 or self.gp_server.backward_gradients != 0:
        #         #     with self.mutex:
        #         logger.info('Backpropagation Started')
        #         if interation != 0:
        #             losses.backward()
        #         clip_grad_norm_(self.gp_server.parameters(), 1.0)
                
        #         self.optimizer.step()
        #         self.optimizer.zero_grad()
        #         logger.info('Backpropagation Successful: Model updated')

        #         nn = self.subtensor.neuron_for_pubkey(self.wallet.hotkey.ss58_address)

        #         self.gp_server.backward_gradients = 0
        #         # --- logging data
        #         wandb_data = {
        #             'block': end_block,
        #             'loss': losses.cpu().item()/interation,
        #             'stake': nn.stake,
        #             'rank': nn.rank,
        #             'incentive': nn.incentive,
        #             'trust': nn.trust,
        #             'consensus': nn.consensus,
        #             'incentive': nn.incentive,
        #             'dividends': nn.dividends,
        #             'emission':  nn.emission,
        #         } 
        #         bittensor.__console__.print('[green]Current Status:[/green]', wandb_data)

        #         # Add additional wandb data for axon, metagraph etc.
        #         if self.config.wandb.api_key != 'default':

        #             df = pandas.concat( [
        #                 bittensor.utils.indexed_values_to_dataframe( prefix = 'w_i_{}'.format(nn.uid), index = self.metagraph.uids, values = self.metagraph.W[:, uid] ),
        #                 bittensor.utils.indexed_values_to_dataframe( prefix = 's_i'.format(nn.uid), index = self.metagraph.uids, values = self.metagraph.S ),
        #                 # axon.to_dataframe( metagraph = self.metagraph ),
        #             ], axis = 1)
        #             df['uid'] = df.index
        #             stats_data_table = wandb.Table( dataframe = df ) 
        #             # wandb_info_axon = axon.to_wandb()                
        #             wandb.log( { **wandb_data }, step = current_block )
        #             wandb.log( { 'stats': stats_data_table }, step = current_block )
        #             wandb.log( { 'axon_query_times': wandb.plot.scatter( stats_data_table, "uid", "axon_query_time", title="Axon Query time by UID") } )
        #             wandb.log( { 'in_weights': wandb.plot.scatter( stats_data_table, "uid", 'w_i_{}'.format(nn.uid), title="Inward weights by UID") } )
        #             wandb.log( { 'stake': wandb.plot.scatter( stats_data_table, "uid", 's_i', title="Stake by UID") } )
                    
        #         # Save the model
        #         # self.gp_server.save(self.config.neuron.full_path)
                
        #         if current_block - last_set_block > self.config.neuron.blocks_per_set_weights:
                    
        #             # --- Setting weights
        #             try: 
        #                 last_set_block = current_block
        #                 # Set self weights to maintain activity.
        #                 chain_weights = torch.zeros(self.metagraph.n)
        #                 chain_weights [ uid ] = 1 
        #                 did_set = self.subtensor.set_weights(
        #                     uids=self.metagraph.uids,
        #                     weights = chain_weights,
        #                     wait_for_inclusion = False,
        #                     wallet = self.wallet,
        #                 )
                        
        #                 if did_set:
        #                     logger.success('Successfully set weights on the chain')
        #                 else:
        #    )                 logger.error('Failed to set weights on chain. (Timeout)')
        #             except Exception as e:
        #                 logger.error('Failure setting weights on chain with error: {}', e)


        #         if current_block - last_sync_block > self.config.neuron.metagraph_sync:
        #             self.metagraph.sync()
        #             last_sync_block = current_block


        # except KeyboardInterrupt:
        #     pass
        #     # --- User ended session ----
        #     # axon.stop()
        # except Exception as e:
        #     # --- Unknown error ----
        #     logger.exception('Unknown exception: {} with traceback {}', e, traceback.format_exc())

        # """.log( { 'stake': wandb.plot.scatter( stats_data_table, "uid", 's_i', title="Stake by UID") } )
                    
        #         # Save the model
        #         # self.gp_server.save(self.config.neuron.full_path)
                
        #         if current_block - last_set_block > self.config.neuron.blocks_per_set_weights:
                    
        #             # --- Setting weights
        #             try: 
        #                 last_set_block = current_block
        #                 # Set self weights to maintain activity.
        #                 chain_weights = torch.zeros(self.metagraph.n)
        #                 chain_weights [ uid ] = 1 
        #                 did_set = self.subtensor.set_weights(
        #                     uids=self.metagraph.uids,
        #                     weights = chain_weights,
        #                     wait_for_inclusion = False,
        #                     wallet = self.wallet,
        #                 )
                        
        #                 if did_set:
        #                     logger.success('Successfully set weights on the chain')
        #                 else:
        #    )                 logger.error('Failed to set weights on chain. (Timeout)')
        #             except Exception as e:
        #                 logger.error('Failure setting weights on chain with error: {}', e)


        #         if current_block - last_sync_block > self.config.neuron.metagraph_sync:
        #             self.metagraph.sync()
        #             last_sync_block = current_block


        # except KeyboardInterrupt:
        #     pass
        #     # --- User ended session ----
        #     # axon.stop()
        # except Exception as e:
        #     # --- Unknown error ----
        #     logger.exception('Unknown exception: {} with traceback {}', e, traceback.format_exc())

        # """

class DDPServer:
    def __init__( self, config: 'bittensor.config', gp_server):
        r""" Initializes the neuron with the passed config.
        """
        
        self.config = config
        self.wallet = bittensor.wallet( config = config ).create().register()
        self.subtensor = bittensor.subtensor ( config = self.config )
        ctx = mp.get_context('spawn')
        self.forward_q = ctx.Queue()
        self.output_q = ctx.Queue()
        
        self.axon = bittensor.axon (
            wallet = self.wallet,
            forward_text = self.forward_text,
            backward_text = self.backward_text,
            # blacklist = self.blacklist,
            priority = self.priority
        ) 
    
        self.axon_pipe = DDPAxonPipe(config, gp_server, self.wallet, self.forward_q, self.output_q)
        self.timecheck = {}
        self.subtensor = bittensor.subtensor ( config = self.config )
        self.metagraph = bittensor.metagraph ( config = self.config, subtensor = self.subtensor )
        self.metagraph.sync()
        self.futures = {}
        self.queue_resolve = QueueResolver(
            queue = self.output_q,
            target = self.qr,
        )

    def qr (self):           
        while True:
            success = False 
            while not success:
                output = None
                if not self.forward_q.empty() :
                    future_id, output = self.output_q.get()
                    if output != None:
                        self.futures[future_id].set_result(output)
                        success = True
            time.sleep(0.5)

    # Instantiate the model we are going to serve on the network.
    # Creating a threading lock for updates to the model
    # Define our forward function.
    def forward_text ( self, inputs_x, future = None ):
        r""" Forward function that is called when the axon recieves a forward request from other peers
            Args:
                inputs_x ( :obj:`torch.Tensor`, `required`):
                    torch inputs to be forward processed.

            Returns:
                outputs (:obj:`torch.FloatTensor`):
                    The nucleus's outputs as a torch tensor of shape [batch_size, sequence_len, __network_dim__]
        """ 
        bittensor.logging.success('begining future', sufix = f'{id(future)}')
        future_id = id(future)
        self.futures[future_id] = future
        self.forward_q.put( (future_id, inputs_x) )
        return

    

    # Define our backward function.
    def backward_text (inputs_x, grads_dy ):
        r"""Backwards function that is called when the axon recieves a backwards request from other peers.
            Updates the server parameters with gradients through the chain.

            Args:
                inputs_x ( :obj:`torch.Tensor`, `required`):
                    torch inputs from previous forward call.
                grads_dy ( :obj:`torch.Tensor`, `required`):
                    torch grads of forward output.
                    
        """
        # # -- normalized grads -- 
        # grads_dy = grads_dy/(grads_dy.sum() + 0.00001)
        
        # with mutex:
        #     outputs_y = gp_server.encode_forward( inputs_x.to(gp_server.device) )
        #     with torch.autograd.set_detect_anomaly(True):
        #         torch.autograd.backward (
        #             tensors = [ outputs_y ],
        #             grad_tensors = [ grads_dy.to(gp_server.device) ],
        #             retain_graph=True
        #         )
        #     logger.info('Backwards axon gradient applied')

        # gp_server.backward_gradients += inputs_x.size(0)
       
    def priority(self, pubkey:str, request_type:bittensor.proto.RequestType, inputs_x) -> float:
        r"""Calculates the priority on requests based on stake and size of input

            Args:
                pubkey ( str, `required`):
                    The public key of the caller.
                inputs_x ( :obj:`torch.Tensor`, `required`):
                    torch inputs to be forward processed.
                request_type ( bittensor.proto.RequestType, `required`):
                    the request type ('FORWARD' or 'BACKWARD').
        """        
        uid = self.metagraph.hotkeys.index(pubkey)
        priority = self.metagraph.S[uid].item()/ sys.getsizeof(inputs_x)

        return priority

    def blacklist(pubkey:str, request_type:bittensor.proto.RequestType) -> bool:
        r"""Axon security blacklisting, used to blacklist message from low stake members
            Args:
                pubkey ( str, `required`):
                    The public key of the caller.
                request_type ( bittensor.proto.RequestType, `required`):
                    the request type ('FORWARD' or 'BACKWARD').
        """

        # Check for stake
        def stake_check() -> bool:
            # If we allow non-registered requests return False = not blacklisted.
            is_registered = pubkey in metagraph.hotkeys
            if not is_registered:
                if config.neuron.blacklist_allow_non_registered:
                    return False
                else:
                    return True

            # Check stake.
            uid = metagraph.hotkeys.index(pubkey)
            if request_type == bittensor.proto.RequestType.FORWARD:
                if metagraph.S[uid].item() < config.neuron.blacklist.stake.forward:
                    return True
                else:
                    return False

            elif request_type == bittensor.proto.RequestType.BACKWARD:
                if metagraph.S[uid].item() < config.neuron.blacklist.stake.backward:
                    return True
                else:
                    return False

        # Check for time
        def time_check():
            current_time = datetime.now()
            if pubkey in timecheck.keys():
                prev_time = timecheck[pubkey]
                if current_time - prev_time >= timedelta(seconds=config.neuron.blacklist.time):
                    timecheck[pubkey] = current_time
                    return False
                else:
                    timecheck[pubkey] = current_time
                    return True
            else:
                timecheck[pubkey] = current_time
                return False

        # Black list or not
        if stake_check() or time_check():
            return True
        else: 
            return False

    def run(self):
        # --  serve axon to the network.
        self.axon.start().serve(subtensor = self.subtensor)
        self.axon_pipe.run_parallel()
        self.axon_pipe.forward_q = self.forward_q

