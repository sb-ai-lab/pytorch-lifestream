import argparse
import logging
import os
import pickle

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def parse_args(args=None):
    parser = argparse.ArgumentParser()

    parser.add_argument('--data_path', type=os.path.abspath)
    parser.add_argument('--trx_files', nargs='+')
    parser.add_argument('--target_files', nargs='*', default=[])

    parser.add_argument('--print_dataset_info', action='store_true', default=True)
    parser.add_argument('--col_client_id', type=str)
    parser.add_argument('--cols_event_time', nargs='+')
    parser.add_argument('--cols_category', nargs='*', default=[])
    parser.add_argument('--cols_log_norm', nargs='*', default=[])
    parser.add_argument('--col_target', required=False, type=str)
    parser.add_argument('--test_size', type=float, default=0.1)
    parser.add_argument('--salt', type=str, default='42')

    parser.add_argument('--output_train_path', type=os.path.abspath)
    parser.add_argument('--output_test_path', type=os.path.abspath)
    parser.add_argument('--output_test_ids_path', type=os.path.abspath)
    parser.add_argument('--log_file', type=os.path.abspath)

    args = parser.parse_args(args)
    logger.info('Parsed args:\n' + '\n'.join([f'  {k:15}: {v}' for k, v in vars(args).items()]))
    return args


def load_source_data(data_path, trx_files):
    data = []
    for file in trx_files:
        file_path = os.path.join(data_path, file)
        df = pd.read_csv(file_path)
        data.append(df)
        logger.info(f'Loaded {len(df)} rows from "{file_path}"')

    data = pd.concat(data, axis=0)
    logger.info(f'Loaded {len(data)} rows in total')
    return data


def pd_hist(data, name, bins=10):
    if data.dtype.kind == 'f':
        bins = np.linspace(data.min(), data.max(), bins + 1).round(1)
    elif np.percentile(data, 99) - data.min() > bins - 1:
        bins = np.linspace(data.min(), np.percentile(data, 99), bins).astype(int).tolist() + [int(data.max() + 1)]
    else:
        bins = np.arange(data.min(), data.max() + 2, 1).astype(int)
    df = pd.cut(data, bins, right=False).rename(name)
    df = df.to_frame().assign(cnt=1).groupby(name)[['cnt']].sum()
    df['% of total'] = df['cnt'] / df['cnt'].sum()
    return df


def encode_col(col):
    col = col.astype(str)
    return col.map({k: i + 1 for i, k in enumerate(col.value_counts().index)})


def trx_to_features(df_data, print_dataset_info,
                    col_client_id, cols_event_time, cols_category, cols_log_norm):
    def copy_time(rec):
        rec['event_time'] = rec['feature_arrays']['event_time']
        del rec['feature_arrays']['event_time']
        return rec

    if print_dataset_info:
        logger.info(f'Found {df_data[col_client_id].nunique()} unique clients')

    # event_time mapping
    df_event_time = df_data[cols_event_time].drop_duplicates()
    df_event_time = df_event_time.sort_values(cols_event_time)
    df_event_time['event_time'] = np.arange(len(df_event_time))
    df_data = pd.merge(df_data, df_event_time, on=cols_event_time)

    for col in cols_category:
        df_data[col] = encode_col(df_data[col])
        if print_dataset_info:
            logger.info(f'Encoder stat for "{col}":\ncodes | trx_count\n{pd_hist(df_data[col], col)}')

    for col in cols_log_norm:
        df_data[col] = np.log1p(abs(df_data[col])) * np.sign(df_data[col])
        df_data[col] /= abs(df_data[col]).max()
        if print_dataset_info:
            logger.info(f'Encoder stat for "{col}":\ncodes | trx_count\n{pd_hist(df_data[col], col)}')

    if print_dataset_info:
        df = df_data.groupby(col_client_id)['event_time'].count()
        logger.info(f'Trx count per clients:\nlen(trx_list) | client_count\n{pd_hist(df, "trx_count")}')

    logger.info('Feature collection in progress ...')
    features = df_data \
        .assign(et_index=lambda x: x['event_time']) \
        .set_index([col_client_id, 'et_index']).sort_index() \
        .groupby(col_client_id).apply(lambda x: {k: np.array(v) for k, v in x.to_dict(orient='list').items()}) \
        .rename('feature_arrays').reset_index().to_dict(orient='records')

    features = [copy_time(r) for r in features]

    if print_dataset_info:
        feature_names = list(features[0]['feature_arrays'].keys())
        logger.info(f'Feature names: {feature_names}')

    logger.info(f'Prepared features for {len(features)} clients')
    return features


def update_with_target(features, data_path, target_files, col_client_id, col_target):
    df_target = pd.concat([pd.read_csv(os.path.join(data_path, file)) for file in target_files])
    df_target = df_target.set_index(col_client_id)
    d_clients = df_target.to_dict(orient='index')
    logger.info(f'Target loaded for {len(d_clients)} clients')

    features = [
        dict([('target', d_clients.get(rec[col_client_id], {}).get(col_target))] + list(rec.items()))
        for rec in features
    ]
    logger.info(f'Target updated for {len(features)} clients')
    return features


def split_dataset(all_data, test_size, data_path, target_files, col_client_id, salt):
    df_target = pd.concat([pd.read_csv(os.path.join(data_path, file)) for file in target_files])
    s_clients = set(df_target[col_client_id].tolist())

    # shuffle client list
    s_all_data_clients = set(rec[col_client_id] for rec in all_data)
    s_clients = ((cl_id, hash(str(cl_id) + salt)) for cl_id in s_clients if cl_id in s_all_data_clients)
    s_clients = sorted(s_clients, key=lambda x: x[1])
    s_clients = [cl_id for cl_id, _ in s_clients]

    # split client list
    Nrows_test = int(len(s_clients) * test_size)
    s_clients_train = s_clients[:-Nrows_test]
    s_clients_test = s_clients[-Nrows_test:]

    # split data
    labeled_train = [rec for rec in all_data if rec[col_client_id] in s_clients_train]
    labeled_test = [rec for rec in all_data if rec[col_client_id] in s_clients_test]
    unlabeled = [rec for rec in all_data if rec[col_client_id] not in s_clients]
    train = labeled_train + unlabeled
    test = labeled_test

    logger.info(f'Train size: {len(train)} clients')
    logger.info(f'Test size: {len(test)} clients')

    return train, test


def save_features(df_data, save_path):
    with open(save_path, 'wb') as f:
        pickle.dump(df_data, f)
    logger.info(f'Saved to: "{save_path}"')


if __name__ == '__main__':
    config = parse_args()

    if config.log_file is not None:
        handlers = [logging.StreamHandler(), logging.FileHandler(config.log_file, mode='w')]
    else:
        handlers = None
    logging.basicConfig(level=logging.INFO, format='%(funcName)-20s   : %(message)s',
                        handlers=handlers)

    source_data = load_source_data(
        data_path=config.data_path,
        trx_files=config.trx_files,
    )

    client_features = trx_to_features(
        df_data=source_data,
        print_dataset_info=config.print_dataset_info,
        col_client_id=config.col_client_id,
        cols_event_time=config.cols_event_time,
        cols_category=config.cols_category,
        cols_log_norm=config.cols_log_norm,
    )

    if len(config.target_files) > 0 and config.col_target is not None:
        client_features = update_with_target(
            features=client_features,
            data_path=config.data_path,
            target_files=config.target_files,
            col_client_id=config.col_client_id,
            col_target=config.col_target,
        )

    if config.test_size > 0:
        train, test = split_dataset(
            all_data=client_features,
            test_size=config.test_size,
            data_path=config.data_path,
            target_files=config.target_files,
            col_client_id=config.col_client_id,
            salt=config.salt,
        )
    else:
        train = client_features

    save_features(
        df_data=train,
        save_path=config.output_train_path,
    )

    if config.test_size > 0:
        save_features(
            df_data=test,
            save_path=config.output_test_path,
        )
        test_ids = pd.DataFrame({config.col_client_id: [rec[config.col_client_id] for rec in test]})
        test_ids.to_csv(config.output_test_ids_path, index=False)
