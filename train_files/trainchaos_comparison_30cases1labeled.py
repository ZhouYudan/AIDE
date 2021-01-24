import sys

sys.path.extend(['../'])
import os, time, argparse, random
import numpy as np
import logging
import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
from torch.utils.data import DataLoader
from torch.optim import Adam
from tqdm import tqdm
from torch.optim.lr_scheduler import StepLR
import pandas as pd
import torch.nn.functional as F
from skimage import measure
from datasetchaos_comparison import chaos_seg, Compose, Resize, ToTensor, Normalize
from models_twomodalinputs import dcesat2saFuseUNet2, fuseunetsa, fuseunetsaseparate, fuseunet, dcesat2saFuseUNet2reverse
from utils import CrossEntropyLoss2d, DiceLoss, MulticlassDiceLoss, CEMDiceLoss, PolyLR, \
    MulticlassDice_fn, MulticlassAccuracy_fn, Dice_fn


def parse_args():
    parser = argparse.ArgumentParser(description='Segmeantation for CHAOS',
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--model_name', default='fuseunet', type=str, help='fuseunet, ...')
    parser.add_argument('--data_mean', default=None, nargs='+', type=float,
                        help='Normalize mean')
    parser.add_argument('--data_std', default=None, nargs='+', type=float,
                        help='Normalize std')
    parser.add_argument('--batch_size', default=4, type=int, help='batch_size')
    parser.add_argument('--gpu_order', default='0', type=str, help='gpu order')
    parser.add_argument('--torch_seed', default=2, type=int, help='torch_seed')
    parser.add_argument('--lr', default=1e-4, type=float, help='learning rate')
    parser.add_argument('--num_epoch', default=50, type=int, help='num epoch')
    parser.add_argument('--loss', default='cedice', type=str, help='ce, dice')
    parser.add_argument('--img_size', default=256, type=int, help='512')
    parser.add_argument('--lr_policy', default='StepLR', type=str, help='StepLR')
    parser.add_argument('--cedice_weight', default=[1.0, 1.0], nargs='+', type=float,
                        help='weight for ce and dice loss')
    parser.add_argument('--ceclass_weight', default=[1.0, 1.0], nargs='+', type=float,
                        help='categorical weight for ce loss')
    parser.add_argument('--diceclass_weight', default=[1.0, 1.0], nargs='+', type=float,
                        help='categorical weight for dice loss')
    parser.add_argument('--checkpoint', default='checkpoint_chaos_comparison30cases1label/')
    parser.add_argument('--history', default='history_chaos_comparison30cases1label')
    parser.add_argument('--cudnn', default=0, type=int, help='cudnn')
    parser.add_argument('--repetition', default=300, type=int, help='...')

    args = parser.parse_args()
    return args


def record_params(args):
    localtime = time.asctime(time.localtime(time.time()))
    logging.info('Segmeantation for CHAOS MR(Data: {}) \n'.format(localtime))
    logging.info('**************Parameters***************')

    args_dict = args.__dict__
    for key, value in args_dict.items():
        logging.info('{}: {}'.format(key, value))
    logging.info('**************Parameters***************\n')

def build_model(model_name, num_classes):
    if model_name == 'fuseunet':
        net = fuseunet(num_classes=num_classes)
    else:
        raise ValueError('Model not implemented')
    return net


def keep_largest_connected_components(mask):
    out_img = np.zeros(mask.shape, dtype=np.uint8)
    blobs = measure.label(mask, connectivity=1)  # connectivity 1: 4 neighbours 2: 8 neighbours
    props = measure.regionprops(blobs)
    area = [ele.area for ele in props]
    if mask.max() > 0:
        largest_blob_ind = np.argmax(area)
        largest_blob_label = props[largest_blob_ind].label
        out_img[blobs == largest_blob_label] = 1
    return out_img


def one_hot_mask(label, palette):
    semantic_map = []
    for color in palette:
        equality = np.equal(label, color)
        class_map = np.all(equality, axis=-1)
        semantic_map.append(class_map.astype(np.uint8))
    semantic_map = np.stack(semantic_map, axis=-1)
    return semantic_map


def Dice3d_fn(inputs, targets):
    iflat = inputs.reshape(-1)
    tflat = targets.reshape(-1)
    intersection = iflat * tflat
    intersection = 2 * np.sum(intersection)
    union = np.sum(iflat) + np.sum(tflat)
    dice_image = intersection / union
    return dice_image


def Train(train_root, train_csv, test_csv, traincase_csv, testcase_csv, labeledcase_csv):
    # parameters
    args = parse_args()

    train_cases = pd.read_csv(traincase_csv)['patient_case'].tolist()
    test_cases = pd.read_csv(testcase_csv)['patient_case'].tolist()
    label_cases = pd.read_csv(labeledcase_csv)['patient_case'].tolist()
    # record
    record_params(args)

    os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu_order
    torch.manual_seed(args.torch_seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.torch_seed)
    np.random.seed(args.torch_seed)
    random.seed(args.torch_seed)

    if args.cudnn == 0:
        cudnn.benchmark = False
    else:
        cudnn.benchmark = True
        cudnn.deterministic = True
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    num_classes = 2

    net = build_model(args.model_name, num_classes)

    params_name = '{}_r{}.pkl'.format(args.model_name, args.repetition)

    start_epoch = 0
    history = {'train_loss': [], 'test_loss': [],
               'train_dice': [], 'test_dice': []}
    end_epoch = start_epoch + args.num_epoch

    if torch.cuda.device_count() > 1:
        print("Let's use", torch.cuda.device_count(), "GPUs!")
        net = nn.DataParallel(net)
    net.to(device)

    # data
    img_size = args.img_size
    ## train
    train_aug = Compose([
        Resize(size=(img_size, img_size)),
        ToTensor(),
        Normalize(mean=args.data_mean,
                  std=args.data_std)])
    ## test
    test_aug = train_aug

    train_dataset = chaos_seg(root=train_root, csv_file=train_csv, transform=train_aug)
    test_dataset = chaos_seg(root=train_root, csv_file=test_csv, transform=test_aug)

    train_loader = DataLoader(train_dataset, batch_size=args.batch_size,
                              num_workers=4, shuffle=True, drop_last=True)
    test_loader = DataLoader(test_dataset, batch_size=args.batch_size,
                             num_workers=4, shuffle=False)

    # loss function, optimizer and scheduler
    cedice_weight = torch.tensor(args.cedice_weight)
    ceclass_weight = torch.tensor(args.ceclass_weight)
    diceclass_weight = torch.tensor(args.diceclass_weight)

    if args.loss == 'ce':
        criterion = CrossEntropyLoss2d(weight=ceclass_weight).to(device)
    elif args.loss == 'dice':
        criterion = MulticlassDiceLoss(weight=diceclass_weight).to(device)
    elif args.loss == 'cedice':
        criterion = CEMDiceLoss(cediceweight=cedice_weight, ceclassweight=ceclass_weight,
                                diceclassweight=diceclass_weight).to(device)
    else:
        print('Do not have this loss')

    optimizer = Adam(net.parameters(), lr=args.lr, amsgrad=True)

    ## scheduler
    if args.lr_policy == 'StepLR':
        scheduler = StepLR(optimizer, step_size=30, gamma=0.5)
    if args.lr_policy == 'PolyLR':
        scheduler = PolyLR(optimizer, max_epoch=end_epoch, power=0.9)

    # training process
    logging.info('Start Training For CHAOS Seg')
    besttraincasedice = 0.0
    for epoch in range(start_epoch, end_epoch):
        ts = time.time()

        # train
        net.train()
        train_loss = 0.
        train_dice = 0.
        train_count = 0
        for batch_idx, (inphase, outphase, _, targets) in \
                tqdm(enumerate(train_loader), total=int(len(train_loader.dataset) / args.batch_size)):
            inphase = inphase.to(device)
            outphase = outphase.to(device)
            targets = targets[:, 1, :, :].to(device)
            optimizer.zero_grad()
            outputs = net(inphase, outphase)
            loss = criterion(outputs, targets)
            loss.backward()
            optimizer.step()
            train_count += inphase.shape[0]
            train_loss += loss.item() * inphase.shape[0]
            train_dice += Dice_fn(outputs, targets).item()

        train_loss_epoch = train_loss / float(train_count)
        train_dice_epoch = train_dice / float(train_count)
        history['train_loss'].append(train_loss_epoch)
        history['train_dice'].append(train_dice_epoch)

        # test
        net.eval()
        test_loss = 0.
        test_dice = 0.
        test_count = 0

        for batch_idx, (inphase, outphase, _, targets) in tqdm(enumerate(test_loader),
                                                               total=int(len(test_loader.dataset) / args.batch_size)):
            with torch.no_grad():
                inphase = inphase.to(device)
                outphase = outphase.to(device)
                targets = targets[:, 1, :, :].to(device)
                outputs = net(inphase, outphase)
                loss = criterion(outputs, targets)
            test_count += inphase.shape[0]
            test_loss += loss.item() * inphase.shape[0]
            test_dice += Dice_fn(outputs, targets).item()

        test_loss_epoch = test_loss / float(test_count)
        test_dice_epoch = test_dice / float(test_count)
        history['test_loss'].append(test_loss_epoch)
        history['test_dice'].append(test_dice_epoch)

        testcasedices = torch.zeros(len(test_cases))
        startimgslices = torch.zeros(len(test_cases))
        for casecount in tqdm(range(len(test_cases)), total=len(test_cases)):
            caseidx = test_cases[casecount]
            caseinphaseimg = [file for file in test_dataset.t1inphase if int(file.split('/')[0]) == caseidx]
            caseinphaseimg.sort()
            caseoutphaseimg = [file for file in test_dataset.t1outphase if int(file.split('/')[0]) == caseidx]
            caseoutphaseimg.sort()
            casemask = [file for file in test_dataset.masks if int(file.split('/')[0]) == caseidx]
            casemask.sort()
            generatedtarget = []
            target = []
            startcaseimg = int(torch.sum(startimgslices[:casecount + 1]))
            for imgidx in range(len(caseinphaseimg)):
                assert caseinphaseimg[imgidx].split('/')[-1].split('.')[0] == \
                       casemask[imgidx].split('/')[-1].split('.')[0]
                assert caseinphaseimg[imgidx].split('/')[-1].split('-')[1] == \
                       caseoutphaseimg[imgidx].split('/')[-1].split('-')[1]
                assert int(caseinphaseimg[imgidx].split('/')[-1].split('-')[-1].split('.')[0]) == \
                       int(caseoutphaseimg[imgidx].split('/')[-1].split('-')[-1].split('.')[0]) + 1
                sample = test_dataset.__getitem__(imgidx + startcaseimg)
                inphase = sample[0]
                outphase = sample[1]
                mask = sample[3]
                target.append(mask[1, :, :])
                with torch.no_grad():
                    inphase = torch.unsqueeze(inphase.to(device), 0)
                    outphase = torch.unsqueeze(outphase.to(device), 0)
                    output = net(inphase, outphase)
                    output = F.softmax(output, dim=1)
                    output = torch.argmax(output, dim=1)
                    output = output.squeeze().cpu().numpy()
                    generatedtarget.append(output)
            target = np.stack(target, axis=-1)
            generatedtarget = np.stack(generatedtarget, axis=-1)
            generatedtarget_keeplargest = keep_largest_connected_components(generatedtarget)
            testcasedices[casecount] = Dice3d_fn(generatedtarget_keeplargest, target)
            if casecount + 1 < len(test_cases):
                startimgslices[casecount + 1] = len(caseinphaseimg)
        testcasedice = testcasedices.sum() / float(len(test_cases))

        traincasedices = torch.zeros(len(train_cases))
        startimgslices = torch.zeros(len(train_cases))
        generatedmask = []
        for casecount in tqdm(range(len(train_cases)), total=len(train_cases)):
            caseidx = train_cases[casecount]
            caseinphaseimg = [file for file in train_dataset.t1inphase if int(file.split('/')[0]) == caseidx]
            caseinphaseimg.sort()
            caseoutphaseimg = [file for file in train_dataset.t1outphase if int(file.split('/')[0]) == caseidx]
            caseoutphaseimg.sort()
            if caseidx in label_cases:
                casemask = [file for file in train_dataset.masks if file.split('/')[0].isdigit()]
                casemask = [file for file in casemask if int(file.split('/')[0]) == caseidx]
            else:
                casemask = [file for file in train_dataset.masks if file.split('/')[-2].isdigit()]
                casemask = [file for file in casemask if int(file.split('/')[-2]) == caseidx]
            casemask.sort()
            generatedtarget = []
            target = []
            startcaseimg = int(torch.sum(startimgslices[:casecount + 1]))
            for imgidx in range(len(caseinphaseimg)):
                assert caseinphaseimg[imgidx].split('/')[-1].split('.')[0] == \
                       casemask[imgidx].split('/')[-1].split('.')[0]
                assert caseinphaseimg[imgidx].split('/')[-1].split('-')[1] == \
                       caseoutphaseimg[imgidx].split('/')[-1].split('-')[1]
                assert int(caseinphaseimg[imgidx].split('/')[-1].split('-')[-1].split('.')[0]) == \
                       int(caseoutphaseimg[imgidx].split('/')[-1].split('-')[-1].split('.')[0]) + 1
                sample = train_dataset.__getitem__(imgidx + startcaseimg)
                inphase = sample[0]
                outphase = sample[1]
                mask = sample[3]
                target.append(mask[1, :, :])
                with torch.no_grad():
                    inphase = torch.unsqueeze(inphase.to(device), 0)
                    outphase = torch.unsqueeze(outphase.to(device), 0)
                    output = net(inphase, outphase)
                    output = F.softmax(output, dim=1)
                    output = torch.argmax(output, dim=1)
                    output = output.squeeze().cpu().numpy()
                    generatedtarget.append(output)
            target = np.stack(target, axis=-1)
            generatedtarget = np.stack(generatedtarget, axis=-1)
            generatedtarget_keeplargest = keep_largest_connected_components(generatedtarget)
            traincasedices[casecount] = Dice3d_fn(generatedtarget_keeplargest, target)
            generatedmask.append(generatedtarget_keeplargest)
            if casecount + 1 < len(train_cases):
                startimgslices[casecount + 1] = len(caseinphaseimg)
        traincasedice = traincasedices.sum() / float(len(train_cases))

        if traincasedice > besttraincasedice:
            besttraincasedice = traincasedice
            logging.info('Best Checkpoint {} Saving...'.format(epoch + 1))

            save_model = net
            if torch.cuda.device_count() > 1:
                save_model = list(net.children())[0]
            state = {
                'net': save_model.state_dict(),
                'loss': test_loss_epoch,
                'dice': test_dice_epoch,
                'epoch': epoch + 1,
                'history': history
            }
            savecheckname = os.path.join(args.checkpoint, params_name.split('.pkl')[0] +
                                         '_besttraincasedice.' + params_name.split('.')[-1])
            torch.save(state, savecheckname)

        time_cost = time.time() - ts
        logging.info(
            'epoch[%d/%d]: train_loss: %.3f | test_loss: %.3f | train_dice: %.3f | test_dice: %.3f || time: %.1f'
            % (epoch + 1, end_epoch, train_loss_epoch, test_loss_epoch, train_dice_epoch, test_dice_epoch, time_cost))
        logging.info(
            'epoch[%d/%d]: traincase_dice: %.3f | testcase_dice: %.3f || time: %.1f'
            % (epoch + 1, end_epoch, traincasedice, testcasedice, time_cost))

        if args.lr_policy != 'None':
            scheduler.step()

args = parse_args()
if not os.path.exists(args.checkpoint):
    os.mkdir(args.checkpoint)
if not os.path.exists(args.history):
    os.mkdir(args.history)

log_name = '{}_r{}.log'.format(args.model_name, args.repetition)
logging_save = os.path.join(args.history, log_name)
logging.basicConfig(level=logging.INFO,
                    handlers=[
                        logging.StreamHandler(),
                        logging.FileHandler(logging_save)
                    ])

if __name__ == "__main__":
    args = parse_args()
    train_root = '../../inputs_chaos/All_Sets'
    train_csv = '../../inputs_chaos/All_Sets_split/splitimages_pseudolabels_1pretrain/train_data_30cases.csv'
    test_csv = '../../inputs_chaos/All_Sets_split/splitimages_cleanlabel/val_data_10cases.csv'
    traincase_csv = '../../inputs_chaos/All_Sets_split/splitcases/train_data_30cases.csv'
    testcase_csv = '../../inputs_chaos/All_Sets_split/splitcases/val_data_10cases.csv'
    labeledcase_csv = '../../inputs_chaos/All_Sets_split/splitcases/train_data_1cases.csv'
    Train(train_root, train_csv, test_csv, traincase_csv, testcase_csv, labeledcase_csv)