import torch
from torch import nn


class ConvModule(nn.Module):
    """
    Convolutional block composed of conv->batchnorm->relu.
    """

    def __init__(self, cin=1, cout=1, k=1, s=1, p=0, device="cpu"):
        super(ConvModule, self).__init__()
        self.conv = nn.Conv2d(cin, cout, (k, k), stride=s, padding=p, bias=False).to(
            device
        )
        self.bn = nn.BatchNorm2d(
            cout, eps=0.001, momentum=0.03, affine=True, track_running_stats=True
        ).to(device)
        self.silu = nn.SiLU(inplace=True).to(device)

    def forward(self, x):
        x = self.conv(x)
        x = self.bn(x)
        x = self.silu(x)
        return x


class Bottleneck(nn.Module):
    """
    Bottleneck block componsed of conv->conv->residual connection.
    """

    def __init__(self, c=1, shortcut=False, device="cpu"):
        super(Bottleneck, self).__init__()
        self.conv1 = ConvModule(cin=c, cout=c // 2, k=3, s=1, p=1, device=device)
        self.conv2 = ConvModule(cin=c // 2, cout=c, k=3, s=1, p=1, device=device)
        self.shortcut = shortcut

    def forward(self, x):
        xin = x
        x = self.conv1(x)
        x = self.conv2(x)
        if self.shortcut == True:
            x = xin + x
            return x
        return x


class C2f(nn.Module):
    """
    C2f module (cross-stage partial bottleneck with two convolutions) which combines
    high-level features with contextual information to improve detection accuracy.
    """

    def __init__(self, cin=1, cout=1, depth=1, device="cpu"):
        super(C2f, self).__init__()
        self.cout = cout
        self.depth = depth
        self.convmodule1 = ConvModule(cin=cin, cout=cout, k=1, s=1, p=0, device=device)
        bottleneck = []
        for _ in range(depth):
            bottleneck.append(
                Bottleneck(c=self.cout // 2, shortcut=True, device=device)
            )
        self.bottleneck = nn.Sequential(*bottleneck)
        cin = cout // 2 * (depth + 2)
        self.convmodule2 = ConvModule(cin=cin, cout=cout, k=1, s=1, p=0, device=device)

    def forward(self, x):
        x1 = self.convmodule1(x)
        x1_1, x1_2 = torch.split(x1, self.cout // 2, dim=1)
        x3 = torch.cat([x1_1, x1_2], dim=1)
        for mod in self.bottleneck:
            x2 = mod(x1_2)
            x3 = torch.cat([x3, x2], dim=1)
            x1_2 = x2
        x = self.convmodule2(x3)
        return x


class SPPF(nn.Module):
    """
    Spatial pyramid pooling fast module (SPPF) layer accelerates computation
    by pooling features into a fixed-size map.
    """

    def __init__(self, c=1, device="cpu"):
        super(SPPF, self).__init__()
        self.conv1 = ConvModule(cin=c, cout=c, k=1, s=1, p=0, device=device)
        self.mp1 = nn.MaxPool2d(
            kernel_size=5, stride=1, padding=2, dilation=1, ceil_mode=False
        ).to(device)
        self.mp2 = nn.MaxPool2d(
            kernel_size=5, stride=1, padding=2, dilation=1, ceil_mode=False
        ).to(device)
        self.mp3 = nn.MaxPool2d(
            kernel_size=5, stride=1, padding=2, dilation=1, ceil_mode=False
        ).to(device)
        self.conv2 = ConvModule(cin=c * 4, cout=c, k=1, s=1, p=0, device=device)

    def forward(self, x):
        x = self.conv1(x)
        x1 = self.mp1(x)
        x2 = self.mp2(x1)
        x3 = self.mp3(x2)
        x = torch.cat([x, x1, x2, x3], dim=1)
        x = self.conv2(x)
        return x


class DetectionHead(nn.Module):
    """
    Detection head module, which is decoupled to regression, classification,
    and depth central pixel estimation tasks independently.
    """

    def __init__(self, c=1, reg_max=1, nclass=1, device="cpu"):
        super(DetectionHead, self).__init__()
        d = max(c, reg_max * 4)
        self.bboxconv1 = ConvModule(cin=c, cout=d, k=3, s=1, p=1, device=device)
        self.bboxconv2 = ConvModule(cin=d, cout=d, k=3, s=1, p=1, device=device)
        self.bboxconv3 = nn.Conv2d(
            d, 4 * reg_max, (1, 1), stride=1, padding=0, bias=False
        ).to(device)
        self.clsconv1 = ConvModule(cin=c, cout=d, k=3, s=1, p=1, device=device)
        self.clsconv2 = ConvModule(cin=d, cout=d, k=3, s=1, p=1, device=device)
        self.clsconv3 = nn.Conv2d(
            d, nclass, (1, 1), stride=1, padding=0, bias=False
        ).to(device)
        self.dptconv1 = ConvModule(cin=c, cout=d, k=3, s=1, p=1, device=device)
        self.dptconv2 = ConvModule(cin=d, cout=d, k=3, s=1, p=1, device=device)
        self.dptconv3 = nn.Conv2d(d, 1, (1, 1), stride=1, padding=0, bias=False).to(
            device
        )

    def forward(self, x):
        # bbox branch
        xbbox = self.bboxconv1(x)
        xbbox = self.bboxconv2(xbbox)
        xbbox = self.bboxconv3(xbbox)
        # cls branch
        xcls = self.clsconv1(x)
        xcls = self.clsconv2(xcls)
        xcls = self.clsconv3(xcls)
        # depth branch
        xdpt = self.dptconv1(x)
        xdpt = self.dptconv2(xdpt)
        xdpt = self.dptconv3(xdpt)

        feats = torch.cat([xbbox, xcls, xdpt], dim=1)
        return feats


class ObjectDetector(nn.Module):
    """
    Object Detection model inspired on YOLOv8 from Ultralytics (https://docs.ultralytics.com/models/yolov8/#supported-tasks).
    The features maps has been divided by two respect the nano version,
    in order to reduce model size for edge devices.
    The detection head incorportes a new feature: a decoupled head for
    depth estimation of the central pixel of the regressed bounding boxes.

    Args:
        nclasses (int): number of classes in the classification task of bounding boxes.
        device (string): device to initiate and proccess weights; cpu or cuda.

    Attributes:
        convX (nn.Conv2d): two dimensional convolution layer to extract features along
                           different resolution maps.
        sppf (nn.Module): spatial pyramid pooling fast module.
        c2f_x (nn.Module): cross-stage partial bottleneck module.
        upsample (nn.Upsample): upsampling layer to concatenate features in the neck
                                control connections.
        headX (nn.Module): detection head for different features resolution maps.

    Methods:
        forward(self, x): forward given input along detection model.
    """

    def __init__(self, nclasses=1, reg_max=1, device="cpu"):
        super(ObjectDetector, self).__init__()

        self.conv1 = ConvModule(cin=3, cout=16, k=3, s=2, p=1, device=device)
        self.conv2 = ConvModule(cin=16, cout=32, k=3, s=2, p=1, device=device)
        self.conv3 = ConvModule(cin=32, cout=64, k=3, s=2, p=1, device=device)
        self.conv4 = ConvModule(cin=64, cout=64, k=3, s=2, p=1, device=device)
        self.conv5 = ConvModule(cin=64, cout=64, k=3, s=2, p=1, device=device)
        self.conv6 = ConvModule(cin=64, cout=64, k=3, s=2, p=1, device=device)
        self.conv7 = ConvModule(cin=64, cout=64, k=3, s=2, p=1, device=device)

        self.sppf = SPPF(c=64, device=device)

        self.upsample = nn.Upsample(scale_factor=2, mode="nearest").to(device)

        self.c2f_1 = C2f(cin=32, cout=32, depth=1, device=device)
        self.c2f_2 = C2f(cin=64, cout=64, depth=2, device=device)
        self.c2f_3 = C2f(cin=64, cout=64, depth=2, device=device)
        self.c2f_4 = C2f(cin=64, cout=64, depth=1, device=device)
        self.c2f_5 = C2f(cin=128, cout=64, depth=1, device=device)
        self.c2f_6 = C2f(cin=128, cout=64, depth=1, device=device)
        self.c2f_7 = C2f(cin=128, cout=64, depth=1, device=device)
        self.c2f_8 = C2f(cin=128, cout=64, depth=1, device=device)

        self.head1 = DetectionHead(
            c=64, reg_max=reg_max, nclass=nclasses, device=device
        )
        self.head2 = DetectionHead(
            c=64, reg_max=reg_max, nclass=nclasses, device=device
        )
        self.head3 = DetectionHead(
            c=64, reg_max=reg_max, nclass=nclasses, device=device
        )

        # self.inference = Inference(nclasses=nclasses, stride=torch.tensor([8,16,32]), reg_max=reg_max, device=device)

    def forward(self, x):

        ## ------------------------------ BACKBONE ------------------------------------
        x1 = self.conv1(x)
        x2 = self.conv2(x1)
        c2f_1 = self.c2f_1(x2)
        x3 = self.conv3(c2f_1)
        c2f_2 = self.c2f_2(x3)
        x4 = self.conv4(c2f_2)
        c2f_3 = self.c2f_3(x4)
        x5 = self.conv5(c2f_3)
        c2f_4 = self.c2f_4(x5)
        sppf = self.sppf(c2f_4)

        ## ------------------------------ NECK ------------------------------------
        ## process branch
        up_1 = self.upsample(sppf)
        cat_1 = torch.cat([up_1, c2f_3], dim=1)
        c2f_5 = self.c2f_5(cat_1)
        up_2 = self.upsample(c2f_5)
        cat_2 = torch.cat([up_2, c2f_2], dim=1)
        c2f_6 = self.c2f_6(cat_2)

        ## error feedback branch
        x6 = self.conv6(c2f_6)
        cat_3 = torch.cat([x6, c2f_5], dim=1)
        c2f_7 = self.c2f_7(cat_3)
        x7 = self.conv7(c2f_7)
        cat_4 = torch.cat([x7, sppf], dim=1)
        c2f_8 = self.c2f_8(cat_4)

        ## ------------------------------ HEAD ----------------------------------
        head1 = self.head1(c2f_6)
        head2 = self.head2(c2f_7)
        head3 = self.head3(c2f_8)

        head_detections = (head1, head2, head3)
        # y = self.inference(head_detections)

        return head_detections


class ObjectDetectorV0(nn.Module):
    """
    Object Detection model inspired on YOLOv8 from Ultralytics (https://docs.ultralytics.com/models/yolov8/#supported-tasks).
    The features maps has been divided by two respect the nano version,
    in order to reduce model size for edge devices.
    The detection head incorportes a new feature: a decoupled head for
    depth estimation of the central pixel of the regressed bounding boxes.

    Args:
        nclasses (int): number of classes in the classification task of bounding boxes.
        device (string): device to initiate and proccess weights; cpu or cuda.

    Attributes:
        convX (nn.Conv2d): two dimensional convolution layer to extract features along
                           different resolution maps.
        sppf (nn.Module): spatial pyramid pooling fast module.
        c2f_x (nn.Module): cross-stage partial bottleneck module.
        upsample (nn.Upsample): upsampling layer to concatenate features in the neck
                                control connections.
        headX (nn.Module): detection head for different features resolution maps.

    Methods:
        forward(self, x): forward given input along detection model.
    """

    def __init__(self, nclasses=1, reg_max=1, device="cpu"):
        super(ObjectDetectorV0, self).__init__()

        self.conv1 = ConvModule(cin=3, cout=16, k=3, s=2, p=1, device=device)
        self.conv2 = ConvModule(cin=16, cout=32, k=3, s=2, p=1, device=device)
        self.conv3 = ConvModule(cin=32, cout=64, k=3, s=2, p=1, device=device)
        self.conv4 = ConvModule(cin=64, cout=64, k=3, s=2, p=1, device=device)
        # self.conv5 = ConvModule(cin=64, cout=64, k=3, s=2, p=1, device=device)
        # self.conv6 = ConvModule(cin=64, cout=64, k=3, s=2, p=1, device=device)
        # self.conv7 = ConvModule(cin=64, cout=64, k=3, s=2, p=1, device=device)

        self.sppf = SPPF(c=64, device=device)

        self.upsample = nn.Upsample(scale_factor=2, mode="nearest").to(device)

        self.c2f_1 = C2f(cin=32, cout=32, depth=1, device=device)
        self.c2f_2 = C2f(cin=64, cout=64, depth=2, device=device)
        # self.c2f_3 = C2f(cin=64, cout=64, depth=2, device=device)
        # self.c2f_4 = C2f(cin=64, cout=64, depth=1, device=device)
        self.c2f_5 = C2f(cin=64, cout=64, depth=1, device=device)
        self.c2f_6 = C2f(cin=128, cout=64, depth=1, device=device)
        # self.c2f_7 = C2f(cin=128, cout=64, depth=1, device=device)
        # self.c2f_8 = C2f(cin=128, cout=64, depth=1, device=device)

        self.head1 = DetectionHead(
            c=64, reg_max=reg_max, nclass=nclasses, device=device
        )
        # self.head2 = DetectionHead(c=64, reg_max=reg_max, nclass=nclasses, device=device)
        # self.head3 = DetectionHead(c=64, reg_max=reg_max, nclass=nclasses, device=device)

        # self.inference = Inference(nclasses=nclasses, stride=torch.tensor([8,16,32]), reg_max=reg_max, device=device)

    def forward(self, x):

        ## ------------------------------ BACKBONE ------------------------------------
        x1 = self.conv1(x)
        x2 = self.conv2(x1)
        c2f_1 = self.c2f_1(x2)
        x3 = self.conv3(c2f_1)
        c2f_2 = self.c2f_2(x3)
        x4 = self.conv4(c2f_2)
        # c2f_3 = self.c2f_3(x4)
        # x5 = self.conv5(c2f_3)
        # c2f_4 = self.c2f_4(x5)
        sppf = self.sppf(x4)

        ## ------------------------------ NECK ------------------------------------
        ## process branch
        # up_1 = self.upsample(sppf)
        # cat_1 = torch.cat([up_1, c2f_3], dim=1)
        c2f_5 = self.c2f_5(sppf)
        up_2 = self.upsample(c2f_5)
        cat_2 = torch.cat([up_2, c2f_2], dim=1)
        c2f_6 = self.c2f_6(cat_2)

        ## error feedback branch
        # x6 = self.conv6(c2f_6)
        # cat_3 = torch.cat([x6, c2f_5], dim=1)
        # c2f_7 = self.c2f_7(cat_3)
        # x7 = self.conv7(c2f_7)
        # cat_4 = torch.cat([x7, sppf], dim=1)
        # c2f_8 = self.c2f_8(cat_4)

        ## ------------------------------ HEAD ----------------------------------
        head1 = self.head1(c2f_6)
        # head2 = self.head2(c2f_7)
        # head3 = self.head3(c2f_8)

        # head_detections = (head1, head2, head3)
        head_detections = (head1,)
        # y = self.inference(head_detections)

        return head_detections
