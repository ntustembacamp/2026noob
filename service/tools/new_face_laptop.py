from service.tools.face_recognition_core import SharedFaceRecognitionCore


class FaceRecognition:
    """Laptop 版辨識包裝層：不碰 DB，只處理本機模型與 embedding。"""

    def __init__(
        self,
        model_root: str,
        embedding_path: str,
        gpu_id: int = 0,
        threshold: float = 1.24,
        det_thresh: float = 0.50,
        det_size=(640, 640),
        init_timeout_sec: int = 90,
        stage_logger=None,
    ):
        self.model_name = "antelopev2"
        self.gpu_id = gpu_id
        self.threshold = threshold
        self.det_thresh = det_thresh
        self.det_size = det_size
        self.model_root = str(model_root)
        self.embedding_path = str(embedding_path)
        self.init_timeout_sec = int(init_timeout_sec)
        self.stage_logger = stage_logger
        self._core = SharedFaceRecognitionCore(
            model_root=self.model_root,
            embedding_path=self.embedding_path,
            model_name=self.model_name,
            gpu_id=self.gpu_id,
            threshold=self.threshold,
            det_thresh=self.det_thresh,
            det_size=self.det_size,
            allowed_modules=["detection", "recognition"],
            providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
            fallback_providers=["CPUExecutionProvider"],
            init_timeout_sec=self.init_timeout_sec,
            stage_logger=self.stage_logger,
            enable_probes=True,
            probe_resize_edges=(2048, 1024, 640),
        )
        self.model = self._core.model
        self.faces_embedding = self._core.faces_embedding

    def load_faces_from_pickle(self):
        self._core.load_faces_from_pickle()

    def recognition(self, image):
        return self._core.recognition(image)
