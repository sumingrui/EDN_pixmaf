### Copyright (C) 2017 NVIDIA Corporation. All rights reserved. 
### Licensed under the CC BY-NC-SA 4.0 license (https://creativecommons.org/licenses/by-nc-sa/4.0/legalcode).
import time
from collections import OrderedDict
from options.train_options import TrainOptions
from data.data_loader import CreateDataLoader
from models.models import create_model_fullts
import util.util as util
from util.visualizer import Visualizer
import os
import numpy as np
from PIL import Image
import torch
from torch.autograd import Variable
import cv2
import pickle

from models.pixmaf_net.core.cfgs import cfg,parse_args_extend
from models.pixmaf_net.models.networks import render_smpl, move_dict_to_device

opt = TrainOptions().parse()
parse_args_extend(opt)
# TODO 准备log文件

iter_path = os.path.join(opt.checkpoints_dir, opt.name, 'iter.txt')
if opt.continue_train:
    try:
        start_epoch, epoch_iter = np.loadtxt(iter_path , delimiter=',', dtype=int)
    except:
        start_epoch, epoch_iter = 1, 0
    print('Resuming from epoch %d at iteration %d' % (start_epoch, epoch_iter))        
else:    
    start_epoch, epoch_iter = 1, 0

if opt.debug:
    opt.display_freq = 1
    opt.print_freq = 1
    opt.niter = 1
    opt.niter_decay = 0
    opt.max_dataset_size = 10
    cfg.TRAIN.DEBUG = True

# model = create_model_fullts(opt)
# model.train()
# exit()

if opt.use_pixmaf:  
    data_loader, disc_motion_loader = CreateDataLoader(opt, cfg)
    # motion dataset iter
    disc_motion_iter = iter(disc_motion_loader)
else:
    data_loader = CreateDataLoader(opt, cfg)
dataset = data_loader.load_data()
dataset_size = len(data_loader)
print('#training images = %d' % dataset_size)



""" new residual model """
model = create_model_fullts(opt)
model.train()
visualizer = Visualizer(opt)

total_steps = (start_epoch-1) * dataset_size + epoch_iter    
for epoch in range(start_epoch, opt.niter + opt.niter_decay + 1):
    epoch_start_time = time.time()
    if epoch != start_epoch:
        epoch_iter = epoch_iter % dataset_size
    for i, data in enumerate(dataset, start=epoch_iter):
        '''
        batch_size = 4
        print(data['other_params']['frame_ids'])
        print(data['next_other_params']['frame_ids'])
        tensor([18527, 17760,  9416,  6203])
        tensor([18528, 17761,  9417,  6204])
        '''

        iter_start_time = time.time()
        total_steps += opt.batchSize
        epoch_iter += opt.batchSize

        # whether to collect output images
        save_fake = total_steps % opt.display_freq == 0

        ############## Forward Pass ######################

        # 测试dataset
        '''
        additional_data = data['other_params']
        temp_bboxes = additional_data['bboxes']
        temp_cam = additional_data['pred_cam']
        temp_pose = additional_data['pose']
        temp_betas = additional_data['betas']
        temp_opkp = additional_data['openpose_kp_2d']
        temp_verts = additional_data['verts']
        temp_j3d = additional_data['joints3d']

        print(temp_bboxes.shape)
        print(temp_cam.shape)
        print(temp_pose.shape)
        print(temp_betas.shape)
        print(temp_opkp.shape)
        print(temp_verts.shape)
        print(temp_j3d.shape)
        exit()
        
        torch.Size([1, 4])
        torch.Size([1, 3])
        torch.Size([1, 72])
        torch.Size([1, 10])
        torch.Size([1, 25, 3])
        torch.Size([1, 6890, 3])
        torch.Size([1, 49, 3])
        '''

        if opt.use_pixmaf:  
            try:
                real_motion_samples = next(disc_motion_iter)
            except StopIteration:
                disc_motion_iter = iter(disc_motion_loader)
                real_motion_samples = next(disc_motion_iter)
            move_dict_to_device(real_motion_samples, cfg.DEVICE)

        no_nexts = data['next_label'].dim() > 1 #check if has a next label (last training pair does not have a next label)

        if no_nexts:
            cond_zeros = torch.zeros(data['label'].size()).float()

            losses, generated = model(Variable(data['label']), Variable(data['next_label']), Variable(data['image']), \
                    Variable(data['next_image']), Variable(data['face_coords']), Variable(cond_zeros), \
                    data['other_params'], data['next_other_params'], real_motion_samples, infer=True)

            # sum per device losses
            losses = [ torch.mean(x) if not isinstance(x, int) else x for x in losses ]
            loss_dict = dict(zip(model.module.loss_names, losses))

            # calculate final loss scalar
            '''
                        self.loss_names = ['G_GAN', 'G_GAN_Feat', 'G_VGG', \
                                'G_2DKP', 'G_CAM', 'G_SMPL', 'G_VERTS', \
                                'G_SIL', 'G_MOTION', 'G_SHAPECOH', \
                                'D_real', 'D_fake', 'D_MOTION',\
                                'G_GANface', 'D_realface', 'D_fakeface']
            '''

            loss_D = (loss_dict['D_fake'] + loss_dict['D_real']) * 0.5 + loss_dict['D_MOTION'] +\
                        (loss_dict['D_realface'] + loss_dict['D_fakeface']) * 0.5

            loss_G = loss_dict['G_GAN'] + loss_dict['G_GAN_Feat'] + loss_dict['G_VGG'] + loss_dict['G_GANface'] \
                        + loss_dict['G_2DKP'] + loss_dict['G_CAM'] + loss_dict['G_SMPL'] + loss_dict['G_VERTS']\
                        + loss_dict['G_SIL'] + loss_dict['G_MOTION'] + loss_dict['G_SHAPECOH']

            ############### Backward Pass ####################
            # update generator weights
            model.module.optimizer_G.zero_grad()
            loss_G.backward()
            model.module.optimizer_G.step()

            # update discriminator weights
            if total_steps % cfg.TRAIN.MOT_DISCR.UPDATE_STEPS == 0:
                model.module.optimizer_D.zero_grad()
                loss_D.backward()
                model.module.optimizer_D.step()
            else:
                model.module.optimizer_D_wo_motion.zero_grad()
                loss_D.backward()
                model.module.optimizer_D_wo_motion.step()

            #call(["nvidia-smi", "--format=csv", "--query-gpu=memory.used,memory.free"]) 

            ############## Display results and errors ##########
            ### print out errors
            ### 100 epochs打印一次
            if total_steps % opt.print_freq == 0:
                errors = {}
                if torch.__version__[0] == '1':
                    errors = {k: v.item() if not isinstance(v, int) else v for k, v in loss_dict.items()}
                else:
                    errors = {k: v.data[0] if not isinstance(v, int) else v for k, v in loss_dict.items()}
                t = (time.time() - iter_start_time) / opt.batchSize
                visualizer.print_current_errors(epoch, epoch_iter, errors, t)
                visualizer.plot_current_errors(errors, total_steps)

            ### display output images
            if save_fake:
                syn = generated[0].data[0]
                inputs = torch.cat((data['label'], data['next_label']), dim=3)
                targets = torch.cat((data['image'], data['next_image']), dim=3)
                # render SMPL
                render_img_0, render_img_1 = render_smpl(generated[4], torch.cat((data['other_params']['bboxes'],data['next_other_params']['bboxes']),dim=0), \
                                            [util.tensor2im(data['image'][0]),util.tensor2im(data['next_image'][0])])
                
                web_dir = os.path.join(opt.checkpoints_dir, opt.name, 'web')
                img_dir = os.path.join(web_dir, 'images')
                util.save_image(render_img_0,os.path.join(img_dir, 'human_smpl_%d_%d_0.png'%(epoch,epoch_iter)))
                util.save_image(render_img_1,os.path.join(img_dir, 'human_smpl_%d_%d_1.png'%(epoch,epoch_iter)))
                # cv2.imwrite(os.path.join(img_dir, 'human_smpl_%d_%d_0.png'%(epoch,total_steps)), render_img_0)
                # cv2.imwrite(os.path.join(img_dir, 'human_smpl_%d_%d_1.png'%(epoch,total_steps)), render_img_1)

                visuals = OrderedDict([('input_label', util.tensor2im(inputs[0], normalize=False)),
                                           ('synthesized_image', util.tensor2im(syn)),
                                           ('real_image', util.tensor2im(targets[0]))])
                if opt.face_generator: #display face generator on tensorboard
                    miny, maxy, minx, maxx = data['face_coords'][0]
                    res_face = generated[2].data[0]
                    syn_face = generated[1].data[0]
                    preres = generated[3].data[0]
                    visuals = OrderedDict([('input_label', util.tensor2im(inputs[0], normalize=False)),
                                           ('synthesized_image', util.tensor2im(syn)),
                                           ('synthesized_face', util.tensor2im(syn_face)),
                                           ('residual', util.tensor2im(res_face)),
                                           ('real_face', util.tensor2im(data['image'][0][:, miny:maxy, minx:maxx])),
                                           # ('pre_residual', util.tensor2im(preres)),
                                           # ('pre_residual_face', util.tensor2im(preres[:, miny:maxy, minx:maxx])),
                                           ('input_face', util.tensor2im(data['label'][0][:, miny:maxy, minx:maxx], normalize=False)),
                                           ('real_image', util.tensor2im(targets[0]))])
                visualizer.display_current_results(visuals, epoch, total_steps)

                

        ### save latest model
        if total_steps % opt.save_latest_freq == 0:
            print('saving the latest model (epoch %d, total_steps %d)' % (epoch, total_steps))
            model.module.save('latest')            
            np.savetxt(iter_path, (epoch, epoch_iter), delimiter=',', fmt='%d')
            
            file_best_fits = os.path.join(opt.dataroot, 'best_fits.pkl')  
            with open(file_best_fits,'wb') as f:
                pickle.dump(model.module.best_fits,f,pickle.HIGHEST_PROTOCOL)

            print('At steps: %d, Update OPT data %d times'%(total_steps,model.module.countOPT))

    # end of epoch  
    iter_end_time = time.time()
    print('End of epoch %d / %d \t Time Taken: %d sec' %
          (epoch, opt.niter + opt.niter_decay, time.time() - epoch_start_time))

    ### save model for this epoch
    if epoch % opt.save_epoch_freq == 0:
        print('saving the model at the end of epoch %d, iters %d' % (epoch, total_steps))        
        model.module.save('latest')
        model.module.save(epoch)
        np.savetxt(iter_path, (epoch+1, 0), delimiter=',', fmt='%d')

    ### instead of only training the local enhancer, train the entire network after certain iterations
    if (opt.niter_fix_global != 0) and (epoch == opt.niter_fix_global):
        print('------------- finetuning Local + Global generators jointly -------------')
        model.module.update_fixed_params()

    ### instead of only training the face discriminator, train the entire network after certain iterations
    if (opt.niter_fix_main != 0) and (epoch == opt.niter_fix_main):
        print('------------- traing all the discriminators now and not just the face -------------')
        model.module.update_fixed_params_netD()

    ### linearly decay learning rate after certain iterations
    if epoch > opt.niter:
        model.module.update_learning_rate()
