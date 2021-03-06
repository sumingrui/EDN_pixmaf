import torch
import torch.nn as nn
import numpy as np
from torch.nn import functional as F

from ..core.cfgs import cfg
from ..utils.geometry import rot6d_to_rotmat, projection, rotation_matrix_to_angle_axis
from .maf_extractor import MAF_Extractor
from .smpl import SMPL, SMPL_MODEL_DIR, SMPL_MEAN_PARAMS, H36M_TO_J14
from .networks import Down_Sampling, Up_Sampling

import logging
logger = logging.getLogger(__name__)

# 修改网络
class Regressor(nn.Module):
    def __init__(self, feat_dim, smpl_mean_params):
        super().__init__()

        npose = 24 * 6
        # 输入的特征数
        self.fc1 = nn.Linear(feat_dim + npose + 13, 1024)
        self.drop1 = nn.Dropout()
        self.fc2 = nn.Linear(1024, 1024)
        self.drop2 = nn.Dropout()
        self.decpose = nn.Linear(1024, npose)
        self.decshape = nn.Linear(1024, 10)
        self.deccam = nn.Linear(1024, 3)
        nn.init.xavier_uniform_(self.decpose.weight, gain=0.01)
        nn.init.xavier_uniform_(self.decshape.weight, gain=0.01)
        nn.init.xavier_uniform_(self.deccam.weight, gain=0.01)

        self.smpl = SMPL(
            SMPL_MODEL_DIR,
            batch_size=2,
            create_transl=False
        )

        mean_params = np.load(smpl_mean_params)
        init_pose = torch.from_numpy(mean_params['pose'][:]).unsqueeze(0)
        init_shape = torch.from_numpy(mean_params['shape'][:].astype('float32')).unsqueeze(0)
        init_cam = torch.from_numpy(mean_params['cam']).unsqueeze(0)

        '''
        print(init_pose.shape)
        print(init_shape.shape)
        print(init_cam.shape)
        torch.Size([1, 144])
        torch.Size([1, 10])
        torch.Size([1, 3])
        '''

        self.register_buffer('init_pose', init_pose)
        self.register_buffer('init_shape', init_shape)
        self.register_buffer('init_cam', init_cam)

    def forward(self, x, res, init_pose=None, init_shape=None, init_cam=None, n_iter=1, J_regressor=None):
        batch_size = x.shape[0]

        if init_pose is None:
            init_pose = self.init_pose.expand(batch_size, -1)
        if init_shape is None:
            init_shape = self.init_shape.expand(batch_size, -1)
        if init_cam is None:
            init_cam = self.init_cam.expand(batch_size, -1)

        pred_pose = init_pose
        pred_shape = init_shape
        pred_cam = init_cam
        for i in range(n_iter):
            xc = torch.cat([x, pred_pose, pred_shape, pred_cam], 1)
            xc = self.fc1(xc)
            xc = self.drop1(xc)
            xc = self.fc2(xc)
            xc = self.drop2(xc)
            pred_pose = self.decpose(xc) + pred_pose
            pred_shape = self.decshape(xc) + pred_shape
            pred_cam = self.deccam(xc) + pred_cam

        pred_rotmat = rot6d_to_rotmat(pred_pose).view(batch_size, 24, 3, 3)

        pred_output = self.smpl(
            betas=pred_shape,
            body_pose=pred_rotmat[:, 1:],
            global_orient=pred_rotmat[:, 0].unsqueeze(1),
            pose2rot=False
        )

        pred_vertices = pred_output.vertices
        pred_joints = pred_output.joints
        pred_smpl_joints = pred_output.smpl_joints
        pred_keypoints_2d = projection(pred_joints, pred_cam, res)
        pose = rotation_matrix_to_angle_axis(pred_rotmat.reshape(-1, 3, 3)).reshape(-1, 72)

        if J_regressor is not None:
            pred_joints = torch.matmul(J_regressor, pred_vertices)
            pred_pelvis = pred_joints[:, [0], :].clone()
            pred_joints = pred_joints[:, H36M_TO_J14, :]
            pred_joints = pred_joints - pred_pelvis

        output = {
            'theta'  : torch.cat([pred_cam, pred_shape, pose], dim=1),
            'verts'  : pred_vertices,
            'kp_2d'  : pred_keypoints_2d,
            'kp_3d'  : pred_joints,
            'smpl_kp_3d' : pred_smpl_joints,
            'rotmat' : pred_rotmat,
            'pred_cam': pred_cam,
            'pred_shape': pred_shape,
            'pred_pose': pred_pose,
        }
        return output

    def forward_init(self, x, res, init_pose=None, init_shape=None, init_cam=None, n_iter=1, J_regressor=None):
        batch_size = x.shape[0]
        # 获得初始值
        if init_pose is None:
            init_pose = self.init_pose.expand(batch_size, -1)
        if init_shape is None:
            init_shape = self.init_shape.expand(batch_size, -1)
        if init_cam is None:
            init_cam = self.init_cam.expand(batch_size, -1)

        pred_pose = init_pose
        pred_shape = init_shape
        pred_cam = init_cam

        pred_rotmat = rot6d_to_rotmat(pred_pose.contiguous()).view(batch_size, 24, 3, 3)

        pred_output = self.smpl(
            betas=pred_shape,
            body_pose=pred_rotmat[:, 1:],
            global_orient=pred_rotmat[:, 0].unsqueeze(1),
            pose2rot=False
        )

        pred_vertices = pred_output.vertices
        pred_joints = pred_output.joints
        pred_smpl_joints = pred_output.smpl_joints
        pred_keypoints_2d = projection(pred_joints, pred_cam, res)
        pose = rotation_matrix_to_angle_axis(pred_rotmat.reshape(-1, 3, 3)).reshape(-1, 72)

        if J_regressor is not None:
            pred_joints = torch.matmul(J_regressor, pred_vertices)
            pred_pelvis = pred_joints[:, [0], :].clone()
            pred_joints = pred_joints[:, H36M_TO_J14, :]
            pred_joints = pred_joints - pred_pelvis

        output = {
            'theta'  : torch.cat([pred_cam, pred_shape, pose], dim=1),
            'verts'  : pred_vertices,
            'kp_2d'  : pred_keypoints_2d,
            'kp_3d'  : pred_joints,
            'smpl_kp_3d' : pred_smpl_joints,
            'rotmat' : pred_rotmat,
            'pred_cam': pred_cam,
            'pred_shape': pred_shape,
            'pred_pose': pred_pose,
        }
        return output


class PixMAF(nn.Module):

    def __init__(self, smpl_mean_params=SMPL_MEAN_PARAMS, pretrained=True):
        super().__init__()

        # 特征提取
        # EDN下采样backbone [-1, 1024, 16, 32]
        self.feature_extractor = Down_Sampling(input_nc=6, ngf=64, n_downsampling=4, n_blocks=9)

        # deconv layers
        self.deconv_layers = self._make_deconv_layer()
    
        if cfg.PixMAF.USE_PIXMAF:
            # maf_extractor
            self.maf_extractor = nn.ModuleList()
            for i in range(cfg.PixMAF.N_ITER): # 4
                self.maf_extractor.append(MAF_Extractor(deconv_layer_list=i))
            
            ma_feat_len = self.maf_extractor[-1].Dmap.shape[0] * cfg.PixMAF.MLP_DIM[1][-1] # 431*5 = 2155
            
            grid_size_x = 21
            grid_size_y = 21
            yv, xv = torch.meshgrid([torch.linspace(-1, 1, grid_size_y), torch.linspace(-1, 1, grid_size_x)])
            points_grid = torch.stack([yv.reshape(-1), xv.reshape(-1)]).unsqueeze(0) # torch.Size([1, 2, 441])

            self.register_buffer('points_grid', points_grid)
            grid_feat_len = grid_size_x * grid_size_y * cfg.PixMAF.MLP_DIM[0][-1] # 21*21*5 = 2205

            # regressor
            self.regressor = nn.ModuleList()
            for i in range(cfg.PixMAF.N_ITER): # 4
                if i == 0:
                    ref_infeat_dim = grid_feat_len # 2205
                else:
                    ref_infeat_dim = ma_feat_len # 2155
                self.regressor.append(Regressor(feat_dim=ref_infeat_dim, smpl_mean_params=smpl_mean_params))

        # dp_feat_dim = 256
        # self.with_uv = cfg.LOSS.POINT_REGRESSION_WEIGHTS > 0
        # if cfg.MODEL.PyMAF.AUX_SUPV_ON:
        #     self.dp_head = IUV_predict_layer(feat_dim=dp_feat_dim)

    def _make_deconv_layer(self):
        return Up_Sampling()

    def _crop_feature(self, feature, bbox, step, stage = 'global', device='cuda'):
        '''
        h = 512
        w = 1024
        x1 = bbox[0]-bbox[2]/2
        y1 = bbox[1]-bbox[2]/2
        x2 = bbox[0]+bbox[2]/2
        y2 = bbox[1]+bbox[2]/2

        a = (x2-x1)/w
        b = (y2-y1)/h
        tx = -1+(x1+x2)/w
        ty = -1+(y1+y2)/h

        theta = torch.tensor([
            [a,0,tx],
            [0,b,ty]
        ], dtype=torch.float).to(device)

        # print(feature.shape)

        size = torch.Size((1, feature.shape[1], int(bbox[2]/h*feature.shape[2]), int(bbox[2]/h*feature.shape[2])))
        grid = F.affine_grid(theta.unsqueeze(0), size)
        output = F.grid_sample(feature, grid, mode='bilinear', padding_mode='zeros')
        # print(output.shape)
        '''

        # 修改成不需要grid_sample的过程
        # bbox (4,)
        from math import floor  

        x1 = bbox[0]-bbox[2]/2
        y1 = bbox[1]-bbox[2]/2
        x2 = bbox[0]+bbox[2]/2
        y2 = bbox[1]+bbox[2]/2

        if stage == 'global':
            h = 256
            w = 512
            h_f = 32
            w_f = 64

        base_side = floor(bbox[2]/h*h_f)
        base_x1 = floor(x1/w*w_f)
        base_y1 = floor(y1/h*h_f)
        base_x2 = base_x1 + base_side
        base_y2 = base_y1 + base_side

        f_dim = feature.shape[1]
        f_h = feature.shape[2]
        f_w = feature.shape[3]
        
        # base: [512,32,64]
        k = 2**step        
        lu_x = base_x1 * k
        lu_y = base_y1 * k
        rb_x = base_x2 * k 
        rb_y = base_y2 * k 
        p = max(-lu_x, -lu_y, rb_x-f_w, rb_y-f_h, 0)
        feature = F.pad(feature,(p,p,p,p),'constant',0)
        output = feature[:,:,lu_y+p:rb_y+p,lu_x+p:rb_x+p]

        # print(output.shape)
        return output

    def forward(self, x, init_params, J_regressor=None):

        batch_size = x.shape[0]
        bbox = init_params['bboxes'][0]
        res = int(bbox[2])

        # spatial features and global features
        # [-1, 1024, 16, 32]
        s_feat = self.feature_extractor(x)
        # print('feature_extractor:', s_feat.shape)

        deconv_blocks = [self.deconv_layers[0:3], self.deconv_layers[3:6], self.deconv_layers[6:9], self.deconv_layers[9:12]]
        last_block = self.deconv_layers[12:15]
        out_list = {}

        # initial parameters
        # TODO: remove the initial mesh generation during forward to reduce runtime
        # by generating initial mesh the beforehand: smpl_output = self.init_smpl
        if cfg.PixMAF.USE_PIXMAF:
            assert cfg.PixMAF.N_ITER == 4
            smpl_output = self.regressor[0].forward_init(s_feat, res, J_regressor=J_regressor)
            out_list['smpl_out'] = [smpl_output]
            
        # for visulization
        # vis_feat_list = [s_feat.detach()]

        # parameter predictions
        for rf_i in range(cfg.PixMAF.N_ITER): # 0, 1, 2, 3

            s_feat_i = deconv_blocks[rf_i](s_feat)
            # print('s_feat_i',rf_i,':',s_feat_i.shape)
            s_feat = s_feat_i   
            # vis_feat_list.append(s_feat_i.detach())

            if cfg.PixMAF.USE_PIXMAF:
                pred_cam = smpl_output['pred_cam']
                pred_shape = smpl_output['pred_shape']
                pred_pose = smpl_output['pred_pose']

                pred_cam = pred_cam.detach()
                pred_shape = pred_shape.detach()
                pred_pose = pred_pose.detach()

                # 对s_feat_i进行修剪 TODO 只是一种尝试
                s_feat_i = self._crop_feature(s_feat_i, bbox, rf_i)

                self.maf_extractor[rf_i].im_feat = s_feat_i
                self.maf_extractor[rf_i].cam = pred_cam

                if rf_i == 0:
                    sample_points = torch.transpose(self.points_grid.expand(batch_size, -1, -1), 1, 2) # torch.Size([1, 441, 2])
                    # print('sample_points:', sample_points.shape)
                    ref_feature = self.maf_extractor[rf_i].sampling(sample_points) # torch.Size([1, 2205])
                else:
                    pred_smpl_verts = smpl_output['verts'].detach()
                    # print('pred_smpl_verts:',pred_smpl_verts.shape) # torch.Size([1, 6890, 3])
                    # TODO: use a more sparse SMPL implementation (with 431 vertices) for acceleration
                    pred_smpl_verts_ds = torch.matmul(self.maf_extractor[rf_i].Dmap.unsqueeze(0), pred_smpl_verts) # [B, 431, 3]
                    # print('pred_smpl_verts_ds:',pred_smpl_verts_ds.shape) # torch.Size([1, 431, 3])
                    ref_feature = self.maf_extractor[rf_i](pred_smpl_verts_ds, res) # [B, 431 * n_feat]
            
                # print('Regressor:',rf_i)
                # print(self.regressor[rf_i])
                smpl_output = self.regressor[rf_i](ref_feature, res, pred_pose, pred_shape, pred_cam, n_iter=1, J_regressor=J_regressor)
                out_list['smpl_out'].append(smpl_output)

        result_img = last_block(s_feat)
        out_list['Generator'] = result_img

        # if self.training and cfg.MODEL.PyMAF.AUX_SUPV_ON:
        #     iuv_out_dict = self.dp_head(s_feat)
        #     out_list['dp_out'].append(iuv_out_dict)

        # return out_list, vis_feat_list
        return out_list

def Pixmaf_net(smpl_mean_params, pretrained=True):
    """ Constructs an PixMAF model with Pix2pixHD backbone.
    Args:
        pretrained (bool): If True, returns a model pre-trained on a human
    """
    model = PixMAF(smpl_mean_params, pretrained)

    # print(model)
    # exit()

    return model
