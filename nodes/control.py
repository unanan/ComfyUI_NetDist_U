import time
import json
import torch
import random
import requests
import numpy as np
from PIL import Image
from copy import deepcopy


class FetchRemote():
	def __init__(self):
		pass

	@classmethod
	def INPUT_TYPES(s):
		return {
			"required": {
				"final_image": ("IMAGE",),
				"remote_info": ("REMINFO",),
			},
		}

	RETURN_TYPES = ("IMAGE",)
	FUNCTION = "get_remote_job"
	CATEGORY = "remote"
	TITLE = "Fetch from remote"

	def wait_for_job(self,remote_url,job_id):
		url = remote_url + "history"

		image_data = None
		while not image_data:
			r = requests.get(url)
			r.raise_for_status()
			data = r.json()
			if not data:
				time.sleep(0.5)
				continue
			for i,d in data.items():
				if d["prompt"][3].get("job_id") == job_id:
					image_data = d["outputs"][list(d["outputs"].keys())[-1]].get("images")
			time.sleep(0.5)
		return image_data

	# remote_info can be none, but the node shouldn't exist at that point
	def get_remote_job(self, final_image, remote_info):
		def img_to_torch(img):
			image = img.convert("RGB")
			image = np.array(image).astype(np.float32) / 255.0
			image = torch.from_numpy(image)[None,]
			return image

		if not remote_info["remote_url"] or not remote_info["job_id"]:
			return (torch.empty(0,0,0,0),)

		images = []
		for i in self.wait_for_job(remote_info["remote_url"],remote_info["job_id"]):
			img_url = f"{remote_info['remote_url']}view?filename={i['filename']}&subfolder={i['subfolder']}&type={i['type']}"

			ir = requests.get(img_url, stream=True)
			ir.raise_for_status()
			img = Image.open(ir.raw)
			images.append(img_to_torch(img))

		if len(images) == 0:
			img = Image.new(mode="RGB", size=(768, 768))
			images.append(img_to_torch(img))

		out = images[0]
		for i in images[1:]:
			out = torch.cat((out,i))

		return (out,)


class QueueRemoteChainStart:
	def __init__(self):
		pass
	@classmethod
	def INPUT_TYPES(s):
		return {
			"required": {
				"workflow": (["current"],),
				"trigger": (["on_change", "always"],),
				"batch": ("INT", {"default": 1, "min": 1, "max": 8}),
				"seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
			},
			"hidden": {
				"prompt": "PROMPT",
			},
		}

	RETURN_TYPES = ("REMCHAIN",)
	RETURN_NAMES = ("remote_chain_start",)
	FUNCTION = "chain_start"
	CATEGORY = "remote"
	TITLE = "Queue on remote (start of chain)"

	def chain_start(self, workflow, trigger, batch, seed, prompt):
		remote_chain = {
			"seed": seed+batch,
			"batch": batch,
			"prompt": prompt,
			"current_seed": seed+batch,
			"current_batch": batch,
			"job_id": f"netdist-{time.time()}"
		}
		return(remote_chain,)

	@classmethod
	def IS_CHANGED(self, workflow, trigger, batch, seed, prompt):
		# don't trigger on workflow change, only input change
		uuid = f"W:{workflow},B:{batch},S:{seed}"
		return uuid if trigger == "on_change" else str(time.time())


class QueueRemoteChainEnd:
	def __init__(self):
		pass
	@classmethod
	def INPUT_TYPES(s):
		return {
			"required": {
				"remote_chain_end": ("REMCHAIN",)
			}
		}

	RETURN_TYPES = ("INT", "INT")
	RETURN_NAMES = ("seed", "batch")
	FUNCTION = "chain_end"
	CATEGORY = "remote"
	TITLE = "Queue on remote (end of chain)"

	def chain_end(self, remote_chain_end):
		seed = remote_chain_end["current_seed"]
		batch = remote_chain_end["current_batch"]
		print("###########REMQ",seed,batch)
		return(seed,batch)

	# @classmethod
	# def IS_CHANGED(self, remote_chain_end):
		# uid = f"S:{remote_chain_end['seed']}-B:{remote_chain_end['batch']}"
		# return uid


class QueueRemote:
	def __init__(self):
		pass
	@classmethod
	def INPUT_TYPES(s):
		return {
			"required": {
				"remote_chain": ("REMCHAIN",),
				"remote_url": ("STRING", {
					"multiline": False,
					"default": "http://127.0.0.1:8288/",
				}),
				"system": (["windows", "posix"],),
				"batch_override": ("INT", {"default": 0, "min": 0, "max": 8}),
				"enabled": (["true", "false", "remote"],{"default": "true"}),
			}
		}

	RETURN_TYPES = ("REMCHAIN", "REMINFO")
	RETURN_NAMES = ("remote_chain", "remote_info")
	FUNCTION = "queue_on_remote"
	CATEGORY = "remote"
	TITLE = "Queue on remote"

	def queue_on_remote(self, remote_chain, remote_url, system, batch_override, enabled):
		batch = batch_override if batch_override > 0 else remote_chain["batch"]
		remote_chain["seed"] += batch
		remote_info = { # empty
			"remote_url": None,
			"job_id": None,
		}

		if enabled == "false":
			return(remote_chain, remote_info)
		elif enabled == "remote":
			remote_chain["current_seed"] = remote_chain["seed"] # hasn't run yet
			remote_chain["current_batch"] = batch
			# print(remote_chain)
			return(remote_chain, remote_info) #
		else:
			remote_info["remote_url"] = remote_url
			remote_info["job_id"] = remote_chain["job_id"]

		prompt = deepcopy(remote_chain["prompt"])
		to_del = []
		def recursive_node_deletion(start_node):
			target_nodes = [start_node]
			if start_node not in to_del:
				to_del.append(start_node)
			while len(target_nodes) > 0:
				new_targets = []
				for target in target_nodes:
					for node in prompt.keys():
						inputs = prompt[node].get("inputs")
						if not inputs:
							continue
						for i in inputs.values():
							if type(i) == list:
								if len(i) > 0 and i[0] in to_del:
									if node not in to_del:
										to_del.append(node)
										new_targets.append(node)
				target_nodes += new_targets
				target_nodes.remove(target)

		# find current node and disable all others
		output_src = None
		for i in prompt.keys():
			if prompt[i]["class_type"] == "QueueRemote":
				if prompt[i]["inputs"]["remote_url"] == remote_url:
					prompt[i]["inputs"]["enabled"] = "remote"
					output_src = i
				else:
					prompt[i]["inputs"]["enabled"] = "false"

		output = None
		for i in prompt.keys():
			# only leave current fetch but replace with PreviewImage
			if prompt[i]["class_type"] == "FetchRemote":
				if prompt[i]["inputs"]["remote_info"][0] == output_src:
					output = {
							'inputs': {'images': prompt[i]["inputs"]["final_image"]},
							'class_type': 'PreviewImage',
					}
				recursive_node_deletion(i)
			# do not save output on remote
			if prompt[i]["class_type"] in ["SaveImage","PreviewImage"]:
				recursive_node_deletion(i)
		prompt[str(max([int(x) for x in prompt.keys()])+1)] = output

		if system == "posix":
			for i in prompt.keys():
				if prompt[i]["class_type"] == "LoraLoader":
					prompt[i]["inputs"]["lora_name"] = prompt[i]["inputs"]["lora_name"].replace("\\","/")
				if prompt[i]["class_type"] == "VAELoader":
					prompt[i]["inputs"]["vae_name"] = prompt[i]["inputs"]["vae_name"].replace("\\","/")
				if prompt[i]["class_type"] in ["CheckpointLoader","CheckpointLoaderSimple"]:
					prompt[i]["inputs"]["ckpt_name"] = prompt[i]["inputs"]["ckpt_name"].replace("\\","/")
		for i in to_del:
			del prompt[i]

		data = {
			"prompt": prompt,
			"client_id": "netdist",
			"extra_data": {
				"job_id": remote_info["job_id"],
			}
		}
		ar = requests.post(remote_url+"prompt", json=data)
		ar.raise_for_status()
		return(remote_chain, remote_info)