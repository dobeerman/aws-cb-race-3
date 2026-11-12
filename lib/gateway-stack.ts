import { join } from 'node:path';
import {
  ArnFormat, CfnOutput, Duration, RemovalPolicy, Stack, StackProps,
  aws_ec2 as ec2, aws_ecr_assets as assets, aws_ecs as ecs, aws_iam as iam,
  aws_logs as logs, aws_rds as rds, aws_secretsmanager as secrets,
} from 'aws-cdk-lib';
import { Construct } from 'constructs';

export class GatewayStack extends Stack {
  constructor(scope: Construct, id: string, props: StackProps) {
    super(scope, id, props);

    const vpc = new ec2.Vpc(this, 'Vpc', {
      maxAzs: 2,
      natGateways: 0,
      subnetConfiguration: [
        { name: 'TaskEgress', subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
        { name: 'Database', subnetType: ec2.SubnetType.PRIVATE_ISOLATED, cidrMask: 24 },
      ],
    });
    const taskSecurity = new ec2.SecurityGroup(this, 'TaskSecurity', {
      vpc,
      allowAllOutbound: false,
    });
    taskSecurity.addEgressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(443), 'HTTPS to AWS services');
    const databaseSecurity = new ec2.SecurityGroup(this, 'DatabaseSecurity', {
      vpc,
      allowAllOutbound: false,
      disableInlineRules: true,
    });

    const engine = rds.DatabaseClusterEngine.auroraPostgres({
      version: rds.AuroraPostgresEngineVersion.VER_16_8,
    });
    const database = new rds.DatabaseCluster(this, 'Database', {
      engine,
      vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_ISOLATED },
      securityGroups: [databaseSecurity],
      writer: rds.ClusterInstance.serverlessV2('Writer', { publiclyAccessible: false }),
      serverlessV2MinCapacity: 0.5,
      serverlessV2MaxCapacity: 2,
      storageEncrypted: true,
      credentials: rds.Credentials.fromGeneratedSecret('gateway_admin'),
      defaultDatabaseName: 'litellm',
      parameters: { 'rds.force_ssl': '1' },
      backup: { retention: Duration.days(1) },
      removalPolicy: RemovalPolicy.DESTROY,
      deletionProtection: false,
    });
    database.connections.allowDefaultPortFrom(taskSecurity);

    const masterKey = new secrets.Secret(this, 'MasterKey', {
      generateSecretString: {
        secretStringTemplate: '{}',
        generateStringKey: 'key',
        excludePunctuation: true,
        passwordLength: 48,
      },
    });
    const logGroup = new logs.LogGroup(this, 'GatewayLogs', {
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    const cluster = new ecs.Cluster(this, 'Cluster', { vpc });
    const task = new ecs.FargateTaskDefinition(this, 'Task', {
      cpu: 512,
      memoryLimitMiB: 2048,
    });
    const databaseSecret = database.secret!;
    task.addContainer('gateway', {
      image: ecs.ContainerImage.fromAsset(join(__dirname, '../gateway'), {
        platform: assets.Platform.LINUX_AMD64,
      }),
      logging: ecs.LogDrivers.awsLogs({ logGroup, streamPrefix: 'gateway' }),
      linuxParameters: new ecs.LinuxParameters(this, 'Linux', { initProcessEnabled: true }),
      environment: {
        BEDROCK_REGION: this.region,
        DB_HOST: database.clusterEndpoint.hostname,
      },
      secrets: {
        DB_USER: ecs.Secret.fromSecretsManager(databaseSecret, 'username'),
        DB_PASSWORD: ecs.Secret.fromSecretsManager(databaseSecret, 'password'),
        LITELLM_MASTER_KEY_SUFFIX: ecs.Secret.fromSecretsManager(masterKey, 'key'),
      },
      healthCheck: {
        command: ['CMD', 'python', '-c',
          'import urllib.request; urllib.request.urlopen("http://127.0.0.1:4000/health/readiness", timeout=4).close()'],
        interval: Duration.seconds(30),
        timeout: Duration.seconds(5),
        startPeriod: Duration.seconds(120),
        retries: 3,
      },
    });
    task.addToTaskRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
      resources: [this.formatArn({
        service: 'bedrock',
        account: '',
        resource: 'foundation-model',
        resourceName: 'amazon.nova-micro-v1:0',
        arnFormat: ArnFormat.SLASH_RESOURCE_NAME,
      })],
    }));

    const service = new ecs.FargateService(this, 'Service', {
      cluster,
      taskDefinition: task,
      desiredCount: 1,
      minHealthyPercent: 0,
      maxHealthyPercent: 100,
      circuitBreaker: { rollback: true },
      assignPublicIp: true,
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      securityGroups: [taskSecurity],
      enableExecuteCommand: true,
    });
    service.node.addDependency(database);

    new CfnOutput(this, 'ClusterName', { value: cluster.clusterName });
    new CfnOutput(this, 'ServiceName', { value: service.serviceName });
    new CfnOutput(this, 'MasterKeySecretArn', { value: masterKey.secretArn });
    new CfnOutput(this, 'LogGroupName', { value: logGroup.logGroupName });
  }
}
