import getpass
import logging
import pwd
import sys
import time
from itertools import groupby
from pathlib import Path
from shutil import copyfile
from time import sleep

import click
import yaml
from click import MissingParameter, version_option

from esque import __version__, validation
from esque.cli.helpers import attrgetter, edit_yaml, ensure_approval, isatty
from esque.cli.options import State, default_options, output_format_option
from esque.cli.output import (
    blue_bold,
    bold,
    format_output,
    green_bold,
    pretty,
    pretty_new_topic_configs,
    pretty_topic_diffs,
    pretty_unchanged_topic_configs,
    red_bold,
)
from esque.clients.consumer import ConsumerFactory, consume_to_file_ordered, consume_to_files
from esque.clients.producer import PingProducer, ProducerFactory
from esque.config import PING_GROUP_ID, PING_TOPIC, config_dir, config_path, migration, sample_config_path
from esque.controller.consumergroup_controller import ConsumerGroupController
from esque.errors import ValidationException, TopicDoesNotExistException
from esque.resources.broker import Broker
from esque.resources.topic import Topic, copy_to_local


@click.group(help="esque - an operational kafka tool.", invoke_without_command=True)
@click.option("--recreate-config", is_flag=True, default=False, help="Overwrites the config with the sample config.")
@version_option(__version__)
@default_options
def esque(state: State, recreate_config: bool):
    if recreate_config:
        config_dir().mkdir(exist_ok=True)
        if ensure_approval(f"Should the current config in {config_dir()} get replaced?", no_verify=state.no_verify):
            copyfile(sample_config_path().as_posix(), config_path())


@esque.group(help="Get a quick overview of different resources.")
@default_options
def get(state: State):
    pass


@esque.group(help="Get detailed information about a resource.")
@default_options
def describe(state: State):
    pass


@esque.group(help="Create a new instance of a resource.")
@default_options
def create(state: State):
    pass


@esque.group(help="Delete a resource.")
@default_options
def delete(state: State):
    pass


@esque.group(help="Edit a resource")
@default_options
def edit(state: State):
    pass


@esque.group(help="Configuration-related options")
@default_options
def config(state: State):
    pass


def list_brokers(ctx, args, incomplete):
    state = ctx.ensure_object(State)
    all_broker_hosts_names = [f"{broker.host}:{broker.port}" for broker in Broker.get_all(state.cluster)]
    return [broker for broker in all_broker_hosts_names if broker.startswith(incomplete)]


def list_consumergroups(ctx, args, incomplete):
    state = ctx.ensure_object(State)
    return [
        group
        for group in ConsumerGroupController(state.cluster).list_consumer_groups()
        if group.startswith(incomplete)
    ]


def list_contexts(ctx, args, incomplete):
    state = ctx.ensure_object(State)
    return [context for context in state.config.available_contexts if context.startswith(incomplete)]


def list_topics(ctx, args, incomplete):
    state = ctx.ensure_object(State)
    cluster = state.cluster
    return [
        topic.name for topic in cluster.topic_controller.list_topics(search_string=incomplete, get_topic_objects=False)
    ]


def fallback_to_stdin(ctx, args, value):
    stdin = click.get_text_stream("stdin")
    if not value and not isatty(stdin):
        stdin_arg = stdin.readline().strip()
    else:
        stdin_arg = value
    if not stdin_arg:
        raise MissingParameter("No value specified")

    return stdin_arg


@esque.command("ctx", help="Switch clusters.")
@click.argument("context", required=False, default=None, autocompletion=list_contexts)
@default_options
def ctx(state: State, context: str):
    if not context:
        for c in state.config.available_contexts:
            if c == state.config.current_context:
                click.echo(bold(c))
            else:
                click.echo(c)
    if context:
        state.config.context_switch(context)
        state.config.save()
        click.echo(f"Switched to context: {context}")


@config.command("autocomplete", help="Generate the autocompletion script.")
@default_options
def config_autocomplete(state: State):
    directory = config_dir()
    config_file_name = "autocomplete.sh"
    config_file: Path = directory / config_file_name
    current_shell = pwd.getpwnam(getpass.getuser()).pw_shell.split("/")[-1]
    source_designator = "source" if current_shell in ["bash", "sh"] else "source_zsh"
    default_environment = ".bashrc" if current_shell in ["bash", "sh"] else ".zshrc"
    with open(config_file.absolute(), "w") as config_fd:
        config_fd.write('eval "$(_ESQUE_COMPLETE=' + source_designator + ' esque)"')
    click.echo("Autocompletion script generated to " + green_bold(str(config_file.absolute())))
    click.echo(
        "To use the autocompletion feature, simply source the contents of the script into your environment, e.g."
    )
    click.echo(
        '\t\techo -e "\\nsource '
        + str(config_file.absolute())
        + '" >> '
        + str(pwd.getpwnam(getpass.getuser()).pw_dir)
        + "/"
        + default_environment
    )


@config.command("edit", help="Edit your esque config file.")
@default_options
def config_edit(state: State):
    old_yaml = config_path().read_text()
    new_yaml, _ = edit_yaml(old_yaml, validator=validation.validate_esque_config)
    config_path().write_text(new_yaml)


@config.command("migrate", help="Migrate your config to current version")
@default_options
def config_migrate(state: State):
    new_path, backup = migration.migrate(config_path())
    click.echo(f"Your config has been migrated and is now at {new_path}. A backup has been created at {backup}.")


@create.command("topic")
@click.argument("topic-name", callback=fallback_to_stdin, required=False)
@click.option("-l", "--like", help="Topic to use as template", autocompletion=list_topics, required=False)
@default_options
def create_topic(state: State, topic_name: str, like: str):
    if not ensure_approval("Are you sure?", no_verify=state.no_verify):
        click.echo("Aborted")
        return

    topic_controller = state.cluster.topic_controller
    if like:
        template_config = topic_controller.get_cluster_topic(like)
        topic = Topic(
            topic_name, template_config.num_partitions, template_config.replication_factor, template_config.config
        )
    else:
        topic = Topic(topic_name)
    topic_controller.create_topics([topic])
    click.echo(click.style(f"Topic with name '{topic.name}'' successfully created", fg="green"))


@edit.command("topic")
@click.argument("topic-name", required=True, autocompletion=list_topics)
@default_options
def edit_topic(state: State, topic_name: str):
    controller = state.cluster.topic_controller
    topic = state.cluster.topic_controller.get_cluster_topic(topic_name)

    _, new_conf = edit_yaml(topic.to_yaml(only_editable=True), validator=validation.validate_editable_topic_config)

    local_topic = copy_to_local(topic)
    local_topic.update_from_dict(new_conf)
    diff = controller.diff_with_cluster(local_topic)
    if not diff.has_changes:
        click.echo("Nothing changed")
        return

    click.echo(pretty_topic_diffs({topic_name: diff}))
    if ensure_approval("Are you sure?"):
        controller.alter_configs([local_topic])
    else:
        click.echo("canceled")


@edit.command("consumergroup")
@click.argument("consumer-id", callback=fallback_to_stdin, type=click.STRING, required=True)
@click.option(
    "-t",
    "--topic-name",
    help="Regular expression describing the topic name (default: all subscribed topics)",
    type=click.STRING,
    required=False,
)
@click.option("--offset-to-value", help="Set offset to the specified value", type=click.INT, required=False)
@click.option("--offset-by-delta", help="Shift offset by specified value", type=click.INT, required=False)
@click.option(
    "--offset-to-timestamp",
    help="Set offset to the value closest to the specified message timestamp in the format YYYY-MM-DDTHH:mm:ss (NOTE: this can be a very expensive operation).",
    type=click.STRING,
    required=False,
)
@click.option(
    "--offset-from-group", help="Copy all offsets from an existing consumer group.", type=click.STRING, required=False
)
@default_options
def edit_consumergroup(
    state: State,
    consumer_id: str,
    topic_name: str,
    offset_to_value: int,
    offset_by_delta: int,
    offset_to_timestamp: str,
    offset_from_group: str,
):
    logger = logging.getLogger(__name__)
    consumergroup_controller = ConsumerGroupController(state.cluster)
    offset_plan = consumergroup_controller.create_consumer_group_offset_change_plan(
        consumer_id=consumer_id,
        topic_name=topic_name if topic_name else ".*",
        offset_to_value=offset_to_value,
        offset_by_delta=offset_by_delta,
        offset_to_timestamp=offset_to_timestamp,
        offset_from_group=offset_from_group,
    )

    if offset_plan and len(offset_plan) > 0:
        click.echo(green_bold("Proposed offset changes: "))
        offset_plan.sort(key=attrgetter("topic_name", "partition_id"))
        for topic_name, group in groupby(offset_plan, attrgetter("topic_name")):
            group = list(group)
            max_proposed = max(len(str(elem.proposed_offset)) for elem in group)
            max_current = max(len(str(elem.current_offset)) for elem in group)
            for plan_element in group:
                new_offset = str(plan_element.proposed_offset).rjust(max_proposed)
                format_args = dict(
                    topic_name=plan_element.topic_name,
                    partition_id=plan_element.partition_id,
                    current_offset=plan_element.current_offset,
                    new_offset=new_offset if plan_element.offset_equal else red_bold(new_offset),
                    max_current=max_current,
                )
                click.echo(
                    "Topic: {topic_name}, partition {partition_id:2}, current offset: {current_offset:{max_current}}, new offset: {new_offset}".format(
                        **format_args
                    )
                )
        if ensure_approval("Are you sure?", no_verify=state.no_verify):
            consumergroup_controller.edit_consumer_group_offsets(consumer_id=consumer_id, offset_plan=offset_plan)
    else:
        logger.info("No changes proposed.")
        return


@delete.command("topic")
@click.argument(
    "topic-name", callback=fallback_to_stdin, required=False, type=click.STRING, autocompletion=list_topics
)
@default_options
def delete_topic(state: State, topic_name: str):
    topic_controller = state.cluster.topic_controller
    if ensure_approval("Are you sure?", no_verify=state.no_verify):
        topic_controller.delete_topic(Topic(topic_name))

        assert topic_name not in (t.name for t in topic_controller.list_topics(get_topic_objects=False))

    click.echo(click.style(f"Topic with name '{topic_name}'' successfully deleted", fg="green"))


@esque.command("apply", help="Apply a configuration")
@click.option("-f", "--file", help="Config file path", required=True)
@default_options
def apply(state: State, file: str):
    # Get topic data based on the YAML
    yaml_topic_configs = yaml.safe_load(open(file)).get("topics")
    yaml_topics = [Topic.from_dict(conf) for conf in yaml_topic_configs]
    yaml_topic_names = [t.name for t in yaml_topics]
    if not len(yaml_topic_names) == len(set(yaml_topic_names)):
        raise ValidationException("Duplicate topic names in the YAML!")

    # Get topic data based on the cluster state
    topic_controller = state.cluster.topic_controller
    cluster_topics = topic_controller.list_topics(search_string="|".join(yaml_topic_names))
    cluster_topic_names = [t.name for t in cluster_topics]

    # Calculate changes
    to_create = [yaml_topic for yaml_topic in yaml_topics if yaml_topic.name not in cluster_topic_names]
    to_edit = [
        yaml_topic
        for yaml_topic in yaml_topics
        if yaml_topic not in to_create and topic_controller.diff_with_cluster(yaml_topic).has_changes
    ]
    to_edit_diffs = {t.name: topic_controller.diff_with_cluster(t) for t in to_edit}
    to_ignore = [yaml_topic for yaml_topic in yaml_topics if yaml_topic not in to_create and yaml_topic not in to_edit]

    # Sanity check - the 3 groups of topics should be complete and have no overlap
    assert (
        set(to_create).isdisjoint(set(to_edit))
        and set(to_create).isdisjoint(set(to_ignore))
        and set(to_edit).isdisjoint(set(to_ignore))
        and len(to_create) + len(to_edit) + len(to_ignore) == len(yaml_topics)
    )

    # Print diffs so the user can check
    click.echo(pretty_unchanged_topic_configs(to_ignore))
    click.echo(pretty_new_topic_configs(to_create))
    click.echo(pretty_topic_diffs(to_edit_diffs))

    # Check for actionable changes
    if len(to_edit) + len(to_create) == 0:
        click.echo("No changes detected, aborting")
        return

    # Warn users & abort when replication & num_partition changes are attempted
    if any(not diff.is_valid for _, diff in to_edit_diffs.items()):
        click.echo(
            "Changes to `replication_factor` and `num_partitions` can not be applied on already existing topics"
        )
        click.echo("Cancelling due to invalid changes")
        return

    # Get approval
    if not ensure_approval("Apply changes?", no_verify=state.no_verify):
        click.echo("Cancelling changes")
        return

    # apply changes
    topic_controller.create_topics(to_create)
    topic_controller.alter_configs(to_edit)

    # output confirmation
    changes = {"unchanged": len(to_ignore), "created": len(to_create), "changed": len(to_edit)}
    click.echo(click.style(pretty({"Successfully applied changes": changes}), fg="green"))


@describe.command("topic")
@click.argument(
    "topic-name", callback=fallback_to_stdin, required=False, type=click.STRING, autocompletion=list_topics
)
@click.option(
    "--consumers",
    "-C",
    is_flag=True,
    default=False,
    help=f"Will output the consumer groups reading from this topic. "
    f"{red_bold('Beware! This can be a really expensive operation.')}",
)
@output_format_option
@default_options
def describe_topic(state: State, topic_name: str, consumers: bool, output_format: str):
    topic = state.cluster.topic_controller.get_cluster_topic(topic_name)

    output_dict = {
        "topic": topic_name,
        "partitions": [partition.as_dict() for partition in topic.partitions],
        "config": topic.config,
    }

    if consumers:
        consumergroup_controller = ConsumerGroupController(state.cluster)
        groups = consumergroup_controller.list_consumer_groups()

        consumergroups = [
            group_name
            for group_name in groups
            if topic_name in consumergroup_controller.get_consumergroup(group_name).topics
        ]

        output_dict["consumergroups"] = consumergroups
    click.echo(format_output(output_dict, output_format))


@get.command("watermarks")
@click.option("-t", "--topic-name", required=False, type=click.STRING, autocompletion=list_topics)
@click.argument("topic-name", required=True, type=click.STRING, callback=fallback_to_stdin)
@output_format_option
@default_options
def get_watermarks(state: State, topic_name: str, output_format: str):
    # TODO: Gathering of all watermarks takes super long
    topics = state.cluster.topic_controller.list_topics(search_string=topic_name)

    watermarks = {topic.name: max(v for v in topic.watermarks.values()) for topic in topics}

    click.echo(format_output(watermarks, output_format))


@describe.command("broker")
@click.argument("broker", metavar="BROKER", callback=fallback_to_stdin, autocompletion=list_brokers, required=False)
@output_format_option
@default_options
def describe_broker(state, broker, output_format):
    if broker.isdigit():
        broker = Broker.from_id(state.cluster, broker).describe()
    elif ":" not in broker:
        broker = Broker.from_host(state.cluster, broker).describe()
    else:
        try:
            host, port = broker.split(":")
            broker = Broker.from_host_and_port(state.cluster, host, int(port)).describe()
        except ValueError:
            raise ValidationException(
                "BROKER must either be the broker id (int), the hostname (str), or in the form 'host:port' (str)"
            )

    click.echo(format_output(broker, output_format))


@describe.command("consumergroup")
@click.argument("consumer-id", callback=fallback_to_stdin, autocompletion=list_consumergroups, required=True)
@click.option(
    "--all-partitions",
    help="List status for all topic partitions instead of just summarizing each topic.",
    default=False,
    is_flag=True,
)
@output_format_option
@default_options
def describe_consumergroup(state: State, consumer_id: str, all_partitions: bool, output_format: str):
    consumer_group = ConsumerGroupController(state.cluster).get_consumergroup(consumer_id)
    consumer_group_desc = consumer_group.describe(verbose=all_partitions)

    click.echo(format_output(consumer_group_desc, output_format))


@get.command("brokers")
@output_format_option
@default_options
def get_brokers(state: State, output_format: str):
    brokers = Broker.get_all(state.cluster)
    broker_ids_and_hosts = [f"{broker.broker_id}: {broker.host}:{broker.port}" for broker in brokers]
    click.echo(format_output(broker_ids_and_hosts, output_format))


@get.command("consumergroups")
@output_format_option
@default_options
def get_consumergroups(state: State, output_format: str):
    groups = ConsumerGroupController(state.cluster).list_consumer_groups()
    click.echo(format_output(groups, output_format))


@get.command("topics")
@click.option("-p", "--prefix", type=click.STRING, autocompletion=list_topics)
@output_format_option
@default_options
def get_topics(state: State, prefix: str, output_format: str):
    topics = state.cluster.topic_controller.list_topics(search_string=prefix, get_topic_objects=False)
    topic_names = [topic.name for topic in topics]
    click.echo(format_output(topic_names, output_format))


@esque.command("consume", help="Consume messages of a topic from one environment to a file or STDOUT")
@click.argument("topic", autocompletion=list_topics)
@click.option("-f", "--from", "from_context", help="Source Context.", autocompletion=list_contexts, type=click.STRING)
@click.option("-m", "--match", help="Message filtering expression.", type=click.STRING)
@click.option("-n", "--numbers", help="Number of messages.", type=click.INT, default=sys.maxsize)
@click.option("--last/--first", help="Start consuming from the earliest or latest offset in the topic.")
@click.option("-a", "--avro", help="Set this flag if the topic contains avro data", is_flag=True)
@click.option(
    "-d", "--directory", metavar="<directory>", help="Sets the directory to write the messages to.", type=click.STRING
)
@click.option(
    "-c",
    "--consumergroup",
    help="Consumergroup to store the offset in",
    type=click.STRING,
    autocompletion=list_consumergroups,
    default=None,
)
@click.option(
    "--preserve-order",
    help="Preserve the order of messages, regardless of their partition",
    default=False,
    is_flag=True,
)
@click.option(
    "--stdout",
    "write_to_stdout",
    help="Write messages to STDOUT or to an automatically generated file.",
    default=False,
    is_flag=True,
)
@default_options
def consume(
    state: State,
    topic: str,
    from_context: str,
    numbers: int,
    match: str,
    last: bool,
    avro: bool,
    directory: str,
    consumergroup: str,
    preserve_order: bool,
    write_to_stdout: bool,
):
    current_timestamp_milliseconds = int(round(time.time() * 1000))
    consumergroup_prefix = "group_for_"

    if directory and write_to_stdout:
        raise ValueError("Cannot write to a directory and STDOUT, please pick one!")
    if topic not in map(attrgetter("name"), state.cluster.topic_controller.list_topics(get_topic_objects=False)):
        raise TopicDoesNotExistException(f"Topic {topic} does not exist!", -1)

    if not consumergroup:
        consumergroup = consumergroup_prefix + topic + "_" + str(current_timestamp_milliseconds)
    if not directory:
        directory = Path() / "messages" / topic / str(current_timestamp_milliseconds)
    working_dir = Path(directory)

    if not from_context:
        from_context = state.config.current_context
    if not write_to_stdout and from_context != state.config.current_context:
        click.echo(f"Switching to context: {from_context}.")
    state.config.context_switch(from_context)

    if not write_to_stdout:
        click.echo("Creating directory " + blue_bold(working_dir.absolute().name) + " if it does not exist.")
        working_dir.mkdir(parents=True, exist_ok=True)
        click.echo("Start consuming from topic " + blue_bold(topic) + " in source context " + blue_bold(from_context))
    if preserve_order:
        partitions = []
        for partition in state.cluster.topic_controller.get_cluster_topic(topic).partitions:
            partitions.append(partition.partition_id)
        total_number_of_consumed_messages = consume_to_file_ordered(
            working_dir=working_dir,
            topic=topic,
            group_id=consumergroup,
            partitions=partitions,
            numbers=numbers,
            avro=avro,
            match=match,
            last=last,
            write_to_stdout=write_to_stdout,
        )
    else:
        total_number_of_consumed_messages = consume_to_files(
            working_dir=working_dir,
            topic=topic,
            group_id=consumergroup,
            numbers=numbers,
            avro=avro,
            match=match,
            last=last,
            write_to_stdout=write_to_stdout,
        )

    if not write_to_stdout:
        click.echo("Output generated to " + blue_bold(directory))
        if total_number_of_consumed_messages == numbers or numbers == sys.maxsize:
            click.echo(blue_bold(str(total_number_of_consumed_messages)) + " messages consumed.")
        else:
            click.echo(
                "Found only "
                + bold(str(total_number_of_consumed_messages))
                + " messages in topic, out of "
                + blue_bold(str(numbers))
                + " required."
            )


@esque.command("produce", help="Produce messages from <directory> based on output from transfer command")
@click.argument("topic", autocompletion=list_topics)
@click.option(
    "-d",
    "--directory",
    metavar="<directory>",
    help="Sets the directory that contains Kafka messages",
    type=click.STRING,
)
@click.option("-t", "--to", "to_context", help="Destination Context", autocompletion=list_contexts, type=click.STRING)
@click.option("-m", "--match", help="Message filtering expression", type=click.STRING)
@click.option("-a", "--avro", help="Set this flag if the topic contains avro data", default=False, is_flag=True)
@click.option(
    "--stdin", "read_from_stdin", help="Read messages from STDIN instead of a directory.", default=False, is_flag=True
)
@click.option(
    "-y",
    "--ignore-errors",
    "ignore_stdin_errors",
    help="When reading from STDIN, use malformed strings as message values (ignore JSON).",
    default=False,
    is_flag=True,
)
@default_options
def produce(
    state: State,
    topic: str,
    to_context: str,
    directory: str,
    avro: bool,
    match: str = None,
    read_from_stdin: bool = False,
    ignore_stdin_errors: bool = False,
):
    if directory is None and not read_from_stdin:
        click.echo("You have to provide the directory or use a --stdin flag")
    else:
        if not to_context:
            to_context = state.config.current_context
        if directory is not None:
            working_dir = Path(directory)
            if not working_dir.exists():
                click.echo("You have to provide an existing directory")
                exit(1)
        state.config.context_switch(to_context)
        stdin = click.get_text_stream("stdin")
        if read_from_stdin and isatty(stdin):
            click.echo(
                "Type the messages to produce, "
                + ("in JSON format, " if not ignore_stdin_errors else "")
                + blue_bold("one per line")
                + ". End with "
                + blue_bold("CTRL+D")
            )
        elif read_from_stdin and not isatty(stdin):
            click.echo("Reading messages from an external source, " + blue_bold("one per line"))
        else:
            click.echo(
                "Producing from directory "
                + directory
                + " to topic "
                + blue_bold(topic)
                + " in target context "
                + blue_bold(to_context)
            )
        producer = ProducerFactory().create_producer(
            topic_name=topic,
            working_dir=working_dir if not read_from_stdin else None,
            avro=avro,
            match=match,
            ignore_stdin_errors=ignore_stdin_errors,
        )
        total_number_of_messages_produced = producer.produce()
        click.echo(
            green_bold(str(total_number_of_messages_produced))
            + " messages successfully produced to context "
            + blue_bold(to_context)
            + " and topic "
            + blue_bold(topic)
            + "."
        )


@esque.command("ping", help="Tests the connection to the kafka cluster.")
@click.option("-t", "--times", help="Number of pings.", default=10)
@click.option("-w", "--wait", help="Seconds to wait between pings.", default=1)
@default_options
def ping(state: State, times: int, wait: int):
    topic_controller = state.cluster.topic_controller
    deltas = []
    try:
        topic_controller.create_topics([Topic(PING_TOPIC)])
        producer = PingProducer(PING_TOPIC)
        consumer = ConsumerFactory().create_ping_consumer(group_id=PING_GROUP_ID, topic_name=PING_TOPIC)
        click.echo(f"Ping with {state.cluster.bootstrap_servers}")

        for i in range(times):
            producer.produce()
            _, delta = consumer.consume()
            deltas.append(delta)
            click.echo(f"m_seq={i} time={delta:.2f}ms")
            sleep(wait)
    except KeyboardInterrupt:
        pass
    finally:
        topic_controller.delete_topic(Topic(PING_TOPIC))
        click.echo("--- statistics ---")
        click.echo(f"{len(deltas)} messages sent/received")
        click.echo(f"min/avg/max = {min(deltas):.2f}/{(sum(deltas) / len(deltas)):.2f}/{max(deltas):.2f} ms")
