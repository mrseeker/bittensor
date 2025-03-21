import binascii
import multiprocessing
import ctypes
import struct
import hashlib
import math
import bittensor
import rich
import time
import torch
import numbers
import pandas
from typing import Any, Tuple, List, Union, Optional


def indexed_values_to_dataframe ( 
        prefix: Union[str, int],
        index: Union[list, torch.LongTensor], 
        values: Union[list, torch.Tensor],
        filter_zeros: bool = False
    ) -> 'pandas.DataFrame':
    # Type checking.
    if not isinstance(prefix, str) and not isinstance(prefix, numbers.Number):
        raise ValueError('Passed prefix must have type str or Number')
    if isinstance(prefix, numbers.Number):
        prefix = str(prefix)
    if not isinstance(index, list) and not isinstance(index, torch.Tensor):
        raise ValueError('Passed uids must have type list or torch.Tensor')
    if not isinstance(values, list) and not isinstance(values, torch.Tensor):
        raise ValueError('Passed values must have type list or torch.Tensor')
    if not isinstance(index, list):
        index = index.tolist()
    if not isinstance(values, list):
        values = values.tolist()

    index = [ idx_i for idx_i in index if idx_i < len(values) and idx_i >= 0 ]
    dataframe = pandas.DataFrame(columns=[prefix], index = index )
    for idx_i in index:
        value_i = values[ idx_i ]
        if value_i > 0 or not filter_zeros:
            dataframe.loc[idx_i] = pandas.Series( { str(prefix): value_i } )
    return dataframe

def unbiased_topk( values, k, dim=0, sorted = True, largest = True):
    r""" Selects topk as in torch.topk but does not bias lower indices when values are equal.
        Args:
            values: (torch.Tensor)
                Values to index into.
            k: (int):
                Number to take.
            
        Return:
            topk: (torch.Tensor):
                topk k values.
            indices: (torch.LongTensor)
                indices of the topk values.
    """
    permutation = torch.randperm(values.shape[ dim ])
    permuted_values = values[ permutation ]
    topk, indices = torch.topk( permuted_values,  k, dim = dim, sorted=sorted, largest=largest )
    return topk, permutation[ indices ]

def hex_bytes_to_u8_list( hex_bytes: bytes ):
    hex_chunks = [int(hex_bytes[i:i+2], 16) for i in range(0, len(hex_bytes), 2)]
    return hex_chunks

def u8_list_to_hex( values: list ):
    total = 0
    for val in reversed(values):
        total = (total << 8) + val
    return total 

def create_seal_hash( block_hash:bytes, nonce:int ) -> bytes:
    nonce_bytes = binascii.hexlify(nonce.to_bytes(8, 'little'))
    block_bytes = block_hash.encode('utf-8')[2:]
    pre_seal = nonce_bytes + block_bytes
    seal = hashlib.sha256( bytearray(hex_bytes_to_u8_list(pre_seal)) ).digest()
    return seal

def seal_meets_difficulty( seal:bytes, difficulty:int ):
    seal_number = int.from_bytes(seal, "big")
    product = seal_number * difficulty
    limit = int(math.pow(2,256) - 1)
    if product > limit:
        return False
    else:
        return True
    
def solve_for_difficulty( block_hash, difficulty ):
    meets = False
    nonce = -1
    while not meets:
        nonce += 1 
        seal = create_seal_hash( block_hash, nonce )
        meets = seal_meets_difficulty( seal, difficulty )
        if nonce > 1:
            break
    return nonce, seal

def solve_for_difficulty_fast( subtensor, wallet, num_processes: int = 5, update_interval: int = 100000 ) -> Tuple[int, int, Any, int, Any]:
    """
    Solves the POW for registration using multiprocessing.
    Args:
        subtensor
            Subtensor to connect to for block information and to submit.
        wallet:
            Wallet to use for registration.
        num_processes: int
            Number of processes to use.
        update_interval: int
            Number of nonces to solve before updating block information.
    Note: 
    - We should modify the number of processes based on user input.
    - We can also modify the update interval to do smaller blocks of work,
        while still updating the block information after a different number of nonces,
        to increase the transparency of the process while still keeping the speed.
    """
    block_number = subtensor.get_current_block()
    difficulty = subtensor.difficulty
    block_hash = subtensor.substrate.get_block_hash( block_number )
    while block_hash == None:
        block_hash = subtensor.substrate.get_block_hash( block_number )
    block_bytes = block_hash.encode('utf-8')[2:]
    
    nonce = -1
    limit = int(math.pow(2,256) - 1)
    start_time = time.time()

    console = bittensor.__console__
    with console.status("Solving") as status:        
        found_solution = multiprocessing.Value('q', -1, lock=False) # int
        best = multiprocessing.Value(ctypes.c_char_p, lock=True) # byte array to get around int size of ctypes
        best.raw = struct.pack("d", float('inf'))
        best_seal = multiprocessing.Array('h', 32, lock=True) # short array should hold bytes (0, 256)
        with multiprocessing.Pool(processes=num_processes, initializer=initProcess_, initargs=(solve_, found_solution, best, best_seal)) as pool:
            # while no solution found and wallet has not been registered
            while found_solution.value == -1 and not wallet.is_registered(subtensor):
                result = pool.starmap(solve_, iterable=[(nonce, block_bytes, difficulty, block_hash, block_number, limit) for nonce in range(update_interval)])
                nonce += update_interval
                itrs_per_sec = update_interval / (time.time() - start_time)
                start_time = time.time()
                difficulty = subtensor.difficulty
                block_number = subtensor.get_current_block()
                block_hash = subtensor.substrate.get_block_hash( block_number)
                while block_hash == None:
                    block_hash = subtensor.substrate.get_block_hash( block_number)
                block_bytes = block_hash.encode('utf-8')[2:]
                with best_seal.get_lock():
                    status.update("Solving\n  Nonce: [bold white]{}[/bold white]\n  Difficulty: [bold white]{}[/bold white]\n  Iters: [bold white]{}/s[/bold white]\n  Block: [bold white]{}[/bold white]\n  Best: [bold white]{}[/bold white]".format( nonce, difficulty, int(itrs_per_sec), block_hash.encode('utf-8'), binascii.hexlify(bytes(best_seal) or bytes(0)) ))
            # exited while, found_solution contains the nonce or wallet is registered
            if found_solution.value == -1: # didn't find solution
                return None, None, None, None, None

            nonce, block_number, block_hash, difficulty, seal = result[found_solution.value]
            return nonce, block_number, block_hash, difficulty, seal

def initProcess_(f, found_solution, best, best_seal):
    f.found = found_solution
    f.best = best
    f.best_seal = best_seal

def solve_(nonce, block_bytes, difficulty, block_hash, block_number, limit):
    # Create seal.
    nonce_bytes = binascii.hexlify(nonce.to_bytes(8, 'little'))
    pre_seal = nonce_bytes + block_bytes
    seal = hashlib.sha256( bytearray(hex_bytes_to_u8_list(pre_seal)) ).digest()
  
    seal_number = int.from_bytes(seal, "big")
    product = seal_number * difficulty

    if product < limit:
        solve_.found.value = nonce
        return (nonce, block_number, block_hash, difficulty, seal)
    with solve_.best.get_lock():
        best_value_as_d = struct.unpack('d', solve_.best.raw)[0]
        if (product - limit) < best_value_as_d:
            
            with solve_.best_seal.get_lock():
                solve_.best.raw = struct.pack('d', product - limit)
                for i in range(32):
                    solve_.best_seal[i] = seal[i]
    return None

def create_pow( subtensor, wallet ):
    nonce, block_number, block_hash, difficulty, seal = solve_for_difficulty_fast( subtensor, wallet )
    return None if nonce is None else {
        'nonce': nonce, 
        'difficulty': difficulty,
        'block_number': block_number, 
        'block_hash': block_hash, 
        'work': binascii.hexlify(seal)
    }
