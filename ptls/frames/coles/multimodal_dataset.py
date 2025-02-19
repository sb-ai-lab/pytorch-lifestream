import numpy as np
import torch
from functools import reduce
from collections import defaultdict
from ptls.data_load.feature_dict import FeatureDict
from ptls.data_load.padded_batch import PaddedBatch
from ptls.data_load.utils import collate_multimodal_feature_dict, get_dict_class_labels
from ptls.frames.coles import MultiModalSortTimeSeqEncoderContainer
 

class MultiModalDataset(FeatureDict, torch.utils.data.Dataset):
    def __init__(
        self,
        data: list,
        splitter: object,
        source_features: list,
        col_id: str,
        source_names: list,
        col_time: str = 'event_time',
        *args, **kwargs
    ):
        """
        Dataset for multimodal learning.

        Args:
            data (list): Concatenated data with feature dicts.
            splitter (object): Object from `ptls.frames.coles.split_strategy`.
                Used to split original sequence into subsequences which are samples from one client.
            source_features (list): List of column names.
            col_id (str): Column name with user_id.
            source_names (list): Column name with name sources, must be specified in the same order as trx_encoders in 
                `ptls.frames.coles.multimodal_module.MultiModalSortTimeSeqEncoderContainer`.
            col_time (str, optional): Column name with event_time. Defaults to 'event_time'.
        """
        super().__init__(*args, **kwargs)
        
        self.data = data
        self.splitter = splitter
        self.col_time = col_time
        self.col_id = col_id
        self.source_names = source_names
        self.source_features = source_features
    
    def __len__(self):
        return len(self.data)
    
    def __getitem__(self, idx):
        feature_arrays = self.data[idx]
        split_data = self.split_source(feature_arrays)
        return self.get_splits(split_data)
    
    def __iter__(self):
        for feature_arrays in self.data:
            split_data = self.split_source(feature_arrays)
            yield self.get_splits(split_data)
            
    def split_source(self, feature_arrays):
        res = defaultdict(dict)
        for feature_name, feature_array in feature_arrays.items():
            if feature_name == self.col_id:
                res[self.col_id] = feature_array
            else:
                source_name, feature_name_transform = self.get_names(feature_name)
                res[source_name][feature_name_transform] = feature_array
        for source in self.source_names:
            if source not in res:
                res[source] = {source_feature: torch.tensor([]) for source_feature in self.source_features[source]}
        return res
    
    def get_names(self, feature_name):
        idx_del = feature_name.find('_')
        return feature_name[:idx_del], feature_name[idx_del + 1:]
                
    
    def get_splits(self, feature_arrays):
        res = {}
        common_local_time = []
        for source_name, feature_dict in feature_arrays.items():
            if source_name != self.col_id:
                local_date = feature_dict[self.col_time]
                common_local_time.extend([(int(loc), ind, source_name) for ind, loc in enumerate(local_date)])
        common_local_time.sort(key=lambda x: x[0])
       
        indexes = self.splitter.split(torch.tensor([x[0] for x in common_local_time]))
        res_ind = []
        for inds in indexes:
            dct = defaultdict(list)
            for ind in inds:
                dct[common_local_time[ind][2]].append(common_local_time[ind][1])
            res_ind.append(dct)  
                
        for source_name, feature_dict in feature_arrays.items():
            if source_name != self.col_id:
                res[source_name] = [{k: v[ix[source_name]] for k, v in feature_dict.items() if self.is_seq_feature(k, v)} for ix in res_ind]
        return res
        
    def collate_fn(self, batch, return_dct_labels=False):
        dict_class_labels = get_dict_class_labels(batch)
        batch = reduce(lambda x, y: {k: x[k] + y[k] for k in x if k in y}, batch)
        padded_batch = collate_multimodal_feature_dict(batch)
        if return_dct_labels:
            return padded_batch, dict_class_labels
        return padded_batch, dict_class_labels[list(dict_class_labels.keys())[0]]

    
class MultiModalIterableDataset(MultiModalDataset, torch.utils.data.IterableDataset):
    pass
