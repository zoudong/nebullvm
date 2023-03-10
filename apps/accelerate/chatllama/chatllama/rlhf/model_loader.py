import os

from beartype.typing import Union, Optional, Tuple

from chatllama.rlhf.config import (
    Config,
    ConfigActor,
    ConfigCritic,
    ConfigReward,
)
from chatllama.rlhf.model_list import hf_models

ConfigType = Union[Config, ConfigActor, ConfigCritic, ConfigReward]


class ModelLoader:
    """Class to load and save models and their checkpoints during training."""

    def __init__(
        self,
    ) -> None:
        pass

    @staticmethod
    def look_for_last_checkpoint(
        model_folder: str,
        model_name: str,
    ) -> Optional[str]:
        """Method to look for the last checkpoint in the model folder
        checkpoint are saved as {model_name}_epoch_{current_epoch}.pt

        Args:
            model_folder (str): the folder where the checkpoints are saved
            model_name (str): the name of the model
        """
        # remove .pt to model name
        model_name = model_name.split(".")[0]
        checkpoints = [
            f for f in os.listdir(model_folder) if f.startswith(model_name)
        ]
        if len(checkpoints) == 0:
            return None
        else:
            checkpoints = sorted(checkpoints)
            last_checkpoint = checkpoints[-1]
            return last_checkpoint

    @staticmethod
    def get_model_path(
        config: ConfigType,
        is_checkpoint: bool = False,
        current_epoch: Optional[int] = None,
    ) -> Tuple[str, str, Optional[str]]:
        """Method to get the path to the right model file. Used when saving
        the model.
        The hierarchy of the model folder is:
        -- model_folder: here store the models trained, for each type of model
                        there is a dedicated folder
            -- actor
            -- critic
            -- reward
            -- actor_rl
            -- checkpoints: here store the checkpoints during training, for
                            each type of model there is a dedicated folder
                -- actor
                -- critic
                -- reward
                -- actor_rl

        Args:
            config (ConfigType): the config object, contains info of the model
            is_checkpoint (bool): if True, the path is for a checkpoint
            current_epoch (Optional[int]): the current epoch, used to create
                the checkpoint name. If is_checkpoint is True, and
                current_epoch is None, return just the folder and the simple
                model name for the possible checkpoint.

        Returns:
            model_folder (str): the folder where the model is saved
            model_name (str): the name of the model
            path (Optional[str]): the path to the model. If is_checkpoint is
                True, and current_epoch is None, return None
        """
        # Get model folder from settings (i.e.  base path for all the models)
        if isinstance(config, ConfigActor) or isinstance(config, ConfigReward):
            model_folder = config.model_folder
        elif isinstance(config, Config):
            model_folder = config.actor.model_folder
        else:
            raise ValueError(
                "Config type not recognized during saving or loading"
            )

        # Add the checkpoint path if necessary
        if is_checkpoint:
            model_folder = os.path.join(model_folder, "checkpoints")

        # Create the folder for the model type
        #  (Actor, Critic, Reward, Actor_RL)
        if isinstance(config, ConfigReward):
            # here use ad-hoc flag from config to distinguish between
            #  reward and critic
            if config.is_reward:
                model_folder = os.path.join(model_folder, "reward")
            else:
                model_folder = os.path.join(model_folder, "critic")
        elif isinstance(config, ConfigActor):
            model_folder = os.path.join(model_folder, "actor")
        elif isinstance(config, Config):
            model_folder = os.path.join(model_folder, "actor_rl")

        # Make the path if not exists
        if os.path.exists(model_folder) is False:
            os.makedirs(model_folder)
            print(f"Model folder does not exist. Creating it: {model_folder}")

        # Create the model name
        model_name = None
        if isinstance(config, Config):
            model_name = config.actor.model
        elif isinstance(config, ConfigReward) or isinstance(
            config, ConfigActor
        ):
            model_name = config.model
        if model_name in hf_models:
            model_name = os.path.split(model_name)[-1]
        if model_name is None:
            raise ValueError("Model name not found")

        # If is a checkpoint and current epoch are available
        # extend the model name with the epoch, if none epoch is provided
        # just return the simple model name
        if is_checkpoint and current_epoch is not None:
            model_name = f"{model_name}_epoch_{current_epoch}.pt"
        else:
            model_name = f"{model_name}.pt"

        # if the epoch is not provided, and it is a checkpoint
        # is impossible to know the path to the file.
        # but we can know the model folder and the model name
        if is_checkpoint and current_epoch is None:
            path = None
        else:
            path = os.path.join(model_folder, model_name)
        return model_folder, model_name, path

    @staticmethod
    def check_model_path(
        config: ConfigType,
        is_checkpoint: bool = False,
        current_epoch: Optional[int] = None,
    ) -> Optional[int]:
        """Method to check if the model path exists to load models
        or checkpoints.

        Args:
            config (ConfigType): the config object, contains info of the model
            is_checkpoint (bool): if True, the path is for a checkpoint
            current_epoch (Optional[int]): the current epoch.
                is is_checkpoint is True, and current_epoch is None,
                it will look for the last checkpoint and return it.

        Returns:
            path (Optional[str]): the path to the model. If is_checkpoint is
                True, and current_epoch is None, search for the last checkpoint
                and return it. If no checkpoint is found, return None.
            epoch (Optional[int]): the epoch of the checkpoint if an actual
                checkpoint is found. If no checkpoint is found, return None.
        """
        model_folder, model_name, path = ModelLoader.get_model_path(
            config,
            is_checkpoint,
            current_epoch,
        )

        # If i am looking for a checkpoint.
        if is_checkpoint and current_epoch is None:
            last_checkpoint = ModelLoader.look_for_last_checkpoint(
                model_folder, model_name
            )
            if last_checkpoint is not None:
                path = os.path.join(model_folder, last_checkpoint)
                # Get the epoch number from the checkpoint name

        if path is not None:
            if os.path.exists(path) is False:
                path = None

        if path is None:
            if is_checkpoint:
                print(
                    f"No previous checkpoint found at "
                    f"{model_folder} for {model_name}"
                )
            else:
                print(
                    f"No previous model found at "
                    f"{model_folder} for model {model_name}"
                )
        else:
            if is_checkpoint:
                # extract epoch from checkpoint name
                epoch = int(last_checkpoint.split("_")[-1].split(".")[0])
                print(f"Found checkpoint for epoch {epoch + 1} ...")
            else:
                print(f"Found model at {path}")
        return path
