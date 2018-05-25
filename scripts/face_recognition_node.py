#!/usr/bin/env python
import cv2
import glob
import os
import pickle
import time
import json

import face_api
import config
import knn

import rospy
from sensor_msgs.msg import Image
from cv_bridge import CvBridge, CvBridgeError
from ros_face_recognition.msg import Box
from ros_face_recognition.srv import Face, Name, NameResponse, FaceResponse, Detect, DetectResponse
from wm_frame_to_box.srv import GetBoundingBoxes3D

from sara_msgs.msg import Faces, FaceMsg, BoundingBox2D, BoundingBoxes2D, BoundingBoxes3D

_topic = config.topic_name
_base_dir = os.path.dirname(__file__)
_face_dir = os.path.join(_base_dir, "faces")

# face_map is a place for objects
# with face id as key and face detail
# as value.
face_map = dict()

classifier = knn.Classifier(k=config.neighbors, thresh=config.dlib_face_threshold)


def name_controller(req):
    response = "FAILED"

    for key, value in face_map:
        if key == req.label:
            face_map[key]["name"] = req.name
            response = "OK"
            break

    return NameResponse(response)


class ImageReader:
    def __init__(self):
        self.time = rospy.get_rostime()
        self.flagClassificationInProgress = False

        self.bridge = CvBridge()
        #self.image_sub = rospy.Subscriber(config.image_topic, Image, self.process, queue_size=1)
        #self.depth_sub = rospy.Subscriber(config.depth_topic, Image, self.process_depth, queue_size=1)

        self.faces_pub = rospy.Publisher('/SaraFaceDetector/face', Faces, queue_size=1)
        self.rgb_pub = rospy.Publisher('/SaraFaceDetector/rgb', Image, queue_size=1)
        self.depth_pub = rospy.Publisher('/SaraFaceDetector/depth', Image, queue_size=1)
        self.msg = Faces()
        self.rgb = Image()
        self.depth =  Image()

        self.faces = []

        self.BB2Dfaces = []
        self.BB3Dfaces = []

        self.frame_limit = 0
        self.face_positions = []
        self.detected_faces = []
        self.known_faces = []

        self.image_shape = (0, 0)


    #def process_depth(self, inputData):
     #   if self.flagClassificationInProgress is not True:
      #      self.flagClassificationInProgress = True
       #     self.depth = inputData
        #    #self.depth_sub.unregister()

    def process(self):
        try:
            self.time = rospy.get_rostime()
            data = rospy.wait_for_message("/head_xtion/rgb/image_raw", Image)
            Depth = rospy.wait_for_message("/head_xtion/depth/image_raw", Image)
            cv_image = self.bridge.imgmsg_to_cv2(data, "bgr8")
            image = cv2.resize(cv_image, (0, 0), fx=config.scale, fy=config.scale)

            self.image_shape = image.shape[:2]

            original = image.copy()

            if self.frame_limit % config.skip_frames == 0:
                self.frame_limit = 0
                # Detecting face positions
                self.face_positions = face_api.detect_faces(image, min_score=config.detection_score,
                                                            max_idx=config.detection_idx)

            self.frame_limit += 1

            if len(self.face_positions) > len(self.detected_faces):
                # Make the list empty
                self.detected_faces = []

                # Compute the 128D vector that describes the face in img identified by
                # shape.
                encodings = face_api.face_descriptor(image, self.face_positions)

                cpt = 0

                for face_position, encoding in zip(self.face_positions, encodings):
                    # Create object from face_position.
                    face = face_api.Face(face_position[0], tracker_timeout=config.tracker_timeout)

                    predicted_id = classifier.predict(encoding)
                    if predicted_id != 0:
                        face.details = face_map[predicted_id]

                        # try to find gender.
                        if face.details["gender"] == "unknown":
                            face.details["gender"] = face_api.predict_gender(encoding)

                    else:
                        face.details["gender"] = face_api.predict_gender(encoding)
                        face_map[face.details["id"]] = face.details

                    if face_map[face.details["id"]]["size"] < config.classification_size:
                        face_map[face.details["id"]]["size"] += 1

                        classifier.add_pair(encoding, face.details["id"])

                        face_path = os.path.join(_face_dir, face.details["id"])
                        if not os.path.exists(face_path):
                            os.mkdir(face_path)
                        with open(os.path.join(face_path, "{}.dump".format(int(time.time()))), 'wb') as fp:
                            pickle.dump(encoding, fp)

                    # Start correlation tracker for face.
                    face.tracker.start_track(image, face_position[0])

                    # Face detection score, The score is bigger for more confident
                    # detections.
                    rospy.loginfo(
                        "Face {}->{:5} , [{}] [score : {:.2f}], [xmin: {} xmax: {} ymin: {} ymax: {}]".format(face.details["id"],
                                                                       face.details["name"],
                                                                       face.details["gender"],
                                                                       face_position[1],
                                                                       face.rect.left()/config.scale,
                                                                       face.rect.width()/config.scale + face.rect.left()/config.scale,
                                                                       face.rect.top()/config.scale,
                                                                       face.rect.height()/config.scale + face.rect.top()/config.scale))

                    face.details["score"] = face_position[1]
                    rospy.loginfo("SCORE {}".format(face.details["score"]))
                    cpt = cpt + 1
                    self.detected_faces.append(face)
            else:
                cpt = 0
                for index, face in enumerate(self.detected_faces):
                    # Update tracker , if quality is low ,
                    # face will be removed from list.
                    if not face.update_tracker(image):
                        self.detected_faces.pop(index)

                    face.details = face_map[str(face.details["id"])]

                    face.draw_face(original)
                    cpt = cpt + 1

            if cpt > 0:
                listBB2D = BoundingBoxes2D()
                for index, face in enumerate(self.detected_faces):
                    msgFace = FaceMsg()
                    msgBB = BoundingBox2D()

                    msgBB.xmin = face.rect.left()/config.scale
                    msgBB.xmax = face.rect.width()/config.scale + face.rect.left()/config.scale
                    msgBB.ymin = face.rect.top()/config.scale
                    msgBB.ymax = face.rect.height()/config.scale + face.rect.top()/config.scale
                    msgBB.Class = "face"

                    msgFace.gender = face.details["gender"]
                    msgFace.id = face.details["id"]
                    msgFace.name = face.details["name"]
                    msgFace.genderProbability = abs(face.details["score"] / 2.7)

                    msgBB.probability = msgFace.genderProbability

                    #msgFace.boundingBoxe = msgBB
                    listBB2D.boundingBoxes.append(msgBB)
                    listBB2D.header.stamp = self.time

                    self.msg.faces.append(msgFace)

                rospy.wait_for_service("/get_3d_bounding_boxes", 1)
                serv = rospy.ServiceProxy("/get_3d_bounding_boxes", GetBoundingBoxes3D)
                resp = serv(listBB2D,Depth, "/head_xtion_depth_frame","/base_link")

                for index, BB3D in enumerate(resp.boundingBoxes3D.boundingBoxes):
                    self.msg.faces[index].boundingBox = BB3D

                self.msg.header.stamp = self.time
                self.faces_pub.publish(self.msg)
                #self.rgb_pub.publish(data)
                #self.depth_pub.publish(Depth)

                self.msg.faces = []
                self.BB3Dfaces = []

            if config.show_window:
                cv2.imshow("image", original)
                key = cv2.waitKey(1) & 0xFF
                if key == ord('q'):
                    cv2.destroyAllWindows()
                    rospy.signal_shutdown("q key pressed")

        except CvBridgeError as e:
            rospy.logerr(e)

        self.flagClassificationInProgress = False
        #self.image_sub.unregister()
        #self.depth_sub = rospy.Subscriber(config.depth_topic, Image, self.process_depth, queue_size=1)

    def service_controller(self, r):
        image_h, image_w = self.image_shape
        boxes = []
        for face in self.detected_faces:
            box = Box()
            box.x = face.rect.left() / float(image_w)
            box.y = face.rect.top() / float(image_h)
            box.w = face.rect.width() / float(image_w)
            box.h = face.rect.height() / float(image_h)
            box.gender = face.details["gender"]
            box.label = face.details["id"]
            box.name = face.details["name"]
            boxes.append(box)

        response = FaceResponse(boxes)
        return response

    def detect_controller(self, req):
        cv_image = self.bridge.imgmsg_to_cv2(req.input_image, "bgr8")
        image = cv2.resize(cv_image, (0, 0), fx=config.scale, fy=config.scale)
        positions = face_api.detect_faces(image, min_score=config.detection_score,
                                          max_idx=config.detection_idx)
        encodings = face_api.face_descriptor(image, positions)

        image_h, image_w = self.image_shape
        boxes = []

        for face_position, encoding in zip(self.face_positions, encodings):
            face = face_api.Face(face_position[0], tracker_timeout=config.tracker_timeout)

            predicted_id = classifier.predict(encoding)
            if predicted_id != 0:
                face.details = face_map[predicted_id]

                # try to find gender.
                if face.details["gender"] == "unknown":
                    face.details["gender"] = face_api.predict_gender(encoding)

            else:
                face.details["gender"] = face_api.predict_gender(encoding)
                face_map[face.details["id"]] = face.details

            if face_map[face.details["id"]]["size"] < config.classification_size:
                face_map[face.details["id"]]["size"] += 1

                classifier.add_pair(encoding, face.details["id"])

                face_path = os.path.join(_face_dir, face.details["id"])
                if not os.path.exists(face_path):
                    os.mkdir(face_path)
                with open(os.path.join(face_path, "{}.dump".format(int(time.time()))), 'wb') as fp:
                    pickle.dump(encoding, fp)

            box = Box()
            box.x = face.rect.left() / float(image_w)
            box.y = face.rect.top() / float(image_h)
            box.w = face.rect.width() / float(image_w)
            box.h = face.rect.height() / float(image_h)
            box.gender = face.details["gender"]
            box.label = face.details["id"]
            box.name = face.details["name"]
            boxes.append(box)
            rospy.loginfo("{} faces loaded.".format(box.name))
        response = DetectResponse(boxes)
        return response


def main():
    rospy.init_node(_topic, anonymous=True)

    try:
        show_window_param = rospy.search_param("show_window")
        tracker_quality_param = rospy.search_param("tracker_quality")
        scale_param = rospy.search_param("scale")
        image_topic = rospy.search_param("image_topic")
        depth_topic = rospy.search_param("depth_topic")
        if image_topic is not None:
            config.image_topic = rospy.get_param(image_topic, config.image_topic)
        if depth_topic is not None:
            config.depth_topic = rospy.get_param(depth_topic, config.depth_topic)
        if show_window_param is not None:
            config.show_window = rospy.get_param(show_window_param, config.show_window)
        if tracker_quality_param is not None:
            config.face_tracker_quality = int(rospy.get_param(tracker_quality_param, config.face_tracker_quality))
        if scale_param is not None:
            config.scale = float(rospy.get_param(scale_param, config.scale))
    except TypeError as err:
        rospy.logerr(err)

    image_reader = ImageReader()

    rospy.loginfo("Reading face database ...")
    # Load face_encodings from files.
    for parent_path in glob.glob(os.path.join(_face_dir, "*")):
        face_id = str(parent_path[-5:])
        if not face_id.isdigit():
            continue
        rospy.loginfo("Loading faces from {} directory ...".format(face_id))
        glob_list = glob.glob(os.path.join(parent_path, "*.dump"))
        for file_path in glob_list:
            print file_path, face_id
            with open(file_path, 'rb') as fp:
                face_encoding = pickle.load(fp)

            classifier.add_pair(face_encoding, face_id)
        rospy.loginfo("Directory {} with {} faces loaded.".format(face_id, len(glob_list)))

    # Load face names.
    faces_json_path = os.path.join(_face_dir, "faces.json")
    if os.path.exists(faces_json_path):
        with open(faces_json_path) as f:
            global face_map
            face_map = json.loads(f.read())

    rospy.loginfo("Listening to images reader")
    rospy.Service('/{}/faces'.format(_topic), Face, image_reader.service_controller)

    rospy.loginfo("Listening to names controller")
    rospy.Service('/{}/names_controller'.format(_topic), Name, name_controller)

    rospy.loginfo("Listening to names controller")
    rospy.Service('/{}/detect'.format(_topic), Detect, image_reader.detect_controller)

    try:
        while 1:
            image_reader.process()
    except KeyboardInterrupt:
        rospy.logwarn("Shutting done ...")
    finally:
        rospy.loginfo("Saving faces.json ...")
        with open(faces_json_path, 'w') as f:
            f.write(json.dumps(face_map))


if __name__ == '__main__':
    main()
