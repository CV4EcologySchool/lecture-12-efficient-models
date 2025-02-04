'''
    Training script. Here, we load the training and validation datasets (and
    data loaders) and the model and train and validate the model accordingly.

    2022 Benjamin Kellenberger
'''

import os
import argparse
import yaml
import glob
from tqdm import trange

import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from torch.optim import SGD

# let's import our own classes and functions!
from util import init_seed, time_sync
from dataset import CTDataset
from model import CustomResNet18



def create_dataloader(cfg, split='train'):
    '''
        Loads a dataset according to the provided split and wraps it in a
        PyTorch DataLoader object.
    '''
    dataset_instance = CTDataset(cfg, split)        # create an object instance of our CTDataset class

    dataLoader = DataLoader(
            dataset=dataset_instance,
            batch_size=cfg['batch_size'],
            shuffle=True,
            num_workers=cfg['num_workers']
        )
    return dataLoader



def load_model_optim_scaler(cfg):
    '''
        Creates a model instance and loads the latest model state weights.
    '''
    model_instance = CustomResNet18(cfg['num_classes'])         # create an object instance of our CustomResNet18 class

    # load latest model state
    model_states = glob.glob('model_states/*.pt')

    # set up model optimizer
    optim = setup_optimizer(cfg, model_instance)

    # Gradient scaling helps prevent gradients with small magnitudes from flushing to zero (“underflowing”)
    # when training with mixed precision. See: https://pytorch.org/tutorials/recipes/recipes/amp_recipe.html
    # Recommended to use one Scaler for the entire run
    scaler = torch.cuda.amp.GradScaler(enabled=cfg.get('use_amp', False))

    if len(model_states):
        # at least one save state found; get latest
        model_epochs = [int(m.replace('model_states/','').replace('.pt','')) for m in model_states]
        start_epoch = max(model_epochs)

        # load state dict and apply weights to model
        print(f'Resuming from epoch {start_epoch}')
        state = torch.load(open(f'model_states/{start_epoch}.pt', 'rb'), map_location='cpu')
        model_instance.load_state_dict(state['model'])
        optim.load_state_dict(state['optim'])
        scaler.load_state_dict(state['scaler'])
    else:
        # no save state found; start anew
        print('Starting new model')
        start_epoch = 0

    return model_instance, optim, scaler, start_epoch



def save_model(cfg, epoch, model, optim, scaler, stats):
    # make sure save directory exists; create if not
    os.makedirs('model_states', exist_ok=True)

    # get model parameters and add to stats...
    stats['model'] = model.state_dict()
    stats['optim'] = optim.state_dict()
    stats['scaler'] = scaler.state_dict()

    # ...and save
    torch.save(stats, open(f'model_states/{epoch}.pt', 'wb'))
    
    # also save config file if not present
    cfpath = 'model_states/config.yaml'
    if not os.path.exists(cfpath):
        with open(cfpath, 'w') as f:
            yaml.dump(cfg, f)

            

def setup_optimizer(cfg, model):
    '''
        The optimizer is what applies the gradients to the parameters and makes
        the model learn on the dataset.
    '''
    optimizer = SGD(model.parameters(),
                    lr=cfg['learning_rate'],
                    weight_decay=cfg['weight_decay'])
    return optimizer



def train(cfg, dataLoader, model, optimizer, scaler):
    '''
        Our actual training function.
    '''

    device = cfg['device']

    # put model on device
    model.to(device)
    
    # put the model into training mode
    # this is required for some layers that behave differently during training
    # and validation (examples: Batch Normalization, Dropout, etc.)
    model.train()

    # loss function
    criterion = nn.CrossEntropyLoss()

    # running averages
    loss_total, oa_total = 0.0, 0.0                         # for now, we just log the loss and overall accuracy (OA)

    # time each component
    dataloader_time = 0.0
    model_time = 0.0
    postprocessing_time = 0.0

    # iterate over dataLoader
    progressBar = trange(len(dataLoader))

    # start timing the dataloader now - each iteration in the for loop
    # calls next(dataLoader), so we need to time its execution
    last_time = time_sync()

    for idx, (data, labels) in enumerate(dataLoader):       # see the last line of file "dataset.py" where we return the image tensor (data) and label

        # put data and labels on device
        data, labels = data.to(device), labels.to(device)

        # data loading is complete
        now = time_sync()
        dataloader_time += (now - last_time)
        last_time = now

        with torch.autocast(device_type=device, dtype=torch.float16, enabled=cfg.get('use_amp', False)):
            # forward pass
            prediction = model(data)

            # loss
            loss = criterion(prediction, labels)

        # backward pass (calculate gradients of current batch)
        # Scales loss.  Calls backward() on scaled loss to create scaled gradients.
        scaler.scale(loss).backward()

        # apply gradients to model parameters
        # scaler.step() first unscales the gradients of the optimizer's assigned params.
        # If these gradients do not contain infs or NaNs, optimizer.step() is then called,
        # otherwise, optimizer.step() is skipped.
        scaler.step(optimizer)

        # Updates the scale for next iteration.
        scaler.update()

        # reset gradients to zero
        optimizer.zero_grad()

        # prediction is complete
        now = time_sync()
        model_time += (now - last_time)
        last_time = now

        # log statistics
        loss_total += loss.item()                       # the .item() command retrieves the value of a single-valued tensor, regardless of its data type and device of tensor

        pred_label = torch.argmax(prediction, dim=1)    # the predicted label is the one at position (class index) with highest predicted value
        oa = torch.mean((pred_label == labels).float()) # OA: number of correct predictions divided by batch size (i.e., average/mean)
        oa_total += oa.item()

        progressBar.set_description(
            '[Train] Loss: {:.2f}; OA: {:.2f}%'.format(
                loss_total/(idx+1),
                100*oa_total/(idx+1)
            )
        )
        progressBar.update(1)

        # postprocessing is complete
        now = time_sync()
        postprocessing_time += (now - last_time)
        last_time = now
    
    # end of epoch; finalize
    progressBar.close()
    loss_total /= len(dataLoader)           # shorthand notation for: loss_total = loss_total / len(dataLoader)
    oa_total /= len(dataLoader)

    print("Dataloader time in seconds:", "%.2f" % dataloader_time)
    print("Model time in seconds:", "%.2f" % model_time)
    print("Postprocessing time in seconds:", "%.2f" % postprocessing_time)

    return loss_total, oa_total



def validate(cfg, dataLoader, model):
    '''
        Validation function. Note that this looks almost the same as the training
        function, except that we don't use any optimizer or gradient steps.
    '''
    
    device = cfg['device']
    model.to(device)

    # put the model into evaluation mode
    # see lines 103-106 above
    model.eval()
    
    criterion = nn.CrossEntropyLoss()   # we still need a criterion to calculate the validation loss

    # running averages
    loss_total, oa_total = 0.0, 0.0     # for now, we just log the loss and overall accuracy (OA)

    # time each component
    dataloader_time = 0.0
    model_time = 0.0
    postprocessing_time = 0.0

    # iterate over dataLoader
    progressBar = trange(len(dataLoader))
    
    # start timing the dataloader now - each iteration in the for loop
    # calls next(dataLoader), so we need to time its execution
    last_time = time_sync()

    with torch.no_grad():               # don't calculate intermediate gradient steps: we don't need them, so this saves memory and is faster
        for idx, (data, labels) in enumerate(dataLoader):

            # put data and labels on device
            data, labels = data.to(device), labels.to(device)

            # data loading is complete
            now = time_sync()
            dataloader_time += (now - last_time)
            last_time = now

            # forward pass
            with torch.autocast(device_type='cuda', dtype=torch.float16, enabled=cfg.get('use_amp', False)):
                prediction = model(data)

            # loss
            loss = criterion(prediction, labels)

            # prediction is complete
            now = time_sync()
            model_time += (now - last_time)
            last_time = now 

            # log statistics
            loss_total += loss.item()

            pred_label = torch.argmax(prediction, dim=1)
            oa = torch.mean((pred_label == labels).float())
            oa_total += oa.item()

            progressBar.set_description(
                '[Val ] Loss: {:.2f}; OA: {:.2f}%'.format(
                    loss_total/(idx+1),
                    100*oa_total/(idx+1)
                )
            )
            progressBar.update(1)

            # postprocessing is complete
            now = time_sync()
            postprocessing_time += (now - last_time)
            last_time = now
    
    # end of epoch; finalize
    progressBar.close()
    loss_total /= len(dataLoader)
    oa_total /= len(dataLoader)

    print("Dataloader time in seconds:", "%.2f" % dataloader_time)
    print("Model time in seconds:", "%.2f" % model_time)
    print("Postprocessing time in seconds:", "%.2f" % postprocessing_time)

    return loss_total, oa_total



def main():

    # Argument parser for command-line arguments:
    # python ct_classifier/train.py --config configs/exp_resnet18.yaml
    parser = argparse.ArgumentParser(description='Train deep learning model.')
    parser.add_argument('--config', help='Path to config file', default='configs/exp_resnet18.yaml')
    args = parser.parse_args()

    # load config
    print(f'Using config "{args.config}"')
    cfg = yaml.safe_load(open(args.config, 'r'))
    print(cfg)

    # init random number generator seed (set at the start)
    init_seed(cfg.get('seed', None))

    # check if GPU is available
    device = cfg['device']
    if device != 'cpu' and not torch.cuda.is_available():
        print(f'WARNING: device set to "{device}" but CUDA not available; falling back to CPU...')
        cfg['device'] = 'cpu'

    # initialize data loaders for training and validation set
    dl_train = create_dataloader(cfg, split='train')
    dl_val = create_dataloader(cfg, split='val')

    # initialize model
    model, optim, scaler, current_epoch = load_model_optim_scaler(cfg)

    # we have everything now: data loaders, model, optimizer; let's do the epochs!
    numEpochs = cfg['num_epochs']
    while current_epoch < numEpochs:
        current_epoch += 1
        print(f'Epoch {current_epoch}/{numEpochs}')

        loss_train, oa_train = train(cfg, dl_train, model, optim, scaler)
        loss_val, oa_val = validate(cfg, dl_val, model)

        # combine stats and save
        stats = {
            'loss_train': loss_train,
            'loss_val': loss_val,
            'oa_train': oa_train,
            'oa_val': oa_val
        }
        save_model(cfg, current_epoch, model, optim, scaler, stats)
    

    # That's all, folks!
        


if __name__ == '__main__':
    # This block only gets executed if you call the "train.py" script directly
    # (i.e., "python ct_classifier/train.py").
    main()
