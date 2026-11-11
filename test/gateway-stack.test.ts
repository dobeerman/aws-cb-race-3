import { test } from 'node:test';
import { App } from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { GatewayStack } from '../lib/gateway-stack';

const stack = new GatewayStack(new App(), 'TestGateway', {
  env: { account: '111111111111', region: 'us-east-1' },
});
const template = Template.fromStack(stack);

test('keeps the gateway closed to inbound network traffic', () => {
  template.resourceCountIs('AWS::EC2::NatGateway', 0);
  template.resourceCountIs('AWS::ElasticLoadBalancingV2::LoadBalancer', 0);
  template.resourceCountIs('AWS::EC2::SecurityGroupIngress', 1);
  const database = template.getResourceId('AWS::RDS::DBCluster');
  const taskGroup = template.getResourceId('AWS::EC2::SecurityGroup', {
    Properties: { GroupDescription: 'TestGateway/TaskSecurity' },
  });
  template.hasResourceProperties('AWS::EC2::SecurityGroupIngress', {
    FromPort: { 'Fn::GetAtt': [database, 'Endpoint.Port'] },
    ToPort: { 'Fn::GetAtt': [database, 'Endpoint.Port'] },
    SourceSecurityGroupId: { 'Fn::GetAtt': [taskGroup, 'GroupId'] },
    CidrIp: Match.absent(),
  });
  template.hasResourceProperties('AWS::EC2::SecurityGroup', {
    GroupDescription: 'TestGateway/TaskSecurity',
    SecurityGroupIngress: Match.absent(),
  });
  template.hasResourceProperties('AWS::IAM::Policy', {
    PolicyDocument: { Statement: Match.arrayWith([Match.objectLike({
      Action: [
        'ssmmessages:CreateControlChannel',
        'ssmmessages:CreateDataChannel',
        'ssmmessages:OpenControlChannel',
        'ssmmessages:OpenDataChannel',
      ],
    })]) },
  });
  template.hasResourceProperties('AWS::ECS::Service', {
    EnableExecuteCommand: true,
    DesiredCount: 1,
    DeploymentConfiguration: {
      MaximumPercent: 100,
      MinimumHealthyPercent: 0,
      DeploymentCircuitBreaker: { Enable: true, Rollback: true },
    },
  });
});

test('provisions one encrypted, nonpublic Aurora Serverless v2 writer', () => {
  template.resourceCountIs('AWS::RDS::DBInstance', 1);
  template.hasResourceProperties('AWS::RDS::DBInstance', {
    DBInstanceClass: 'db.serverless', PubliclyAccessible: false,
  });
  template.hasResourceProperties('AWS::RDS::DBCluster', {
    StorageEncrypted: true,
    Engine: 'aurora-postgresql',
    ServerlessV2ScalingConfiguration: { MinCapacity: 0.5, MaxCapacity: 2 },
  });
});

test('scopes Bedrock inference and injects credentials from Secrets Manager', () => {
  template.hasResourceProperties('AWS::IAM::Policy', {
    PolicyDocument: { Statement: Match.arrayWith([Match.objectLike({
      Action: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
      Resource: { 'Fn::Join': ['', ['arn:', { Ref: 'AWS::Partition' },
        ':bedrock:us-east-1::foundation-model/amazon.nova-micro-v1:0']] },
    })]) },
  });
  template.hasResourceProperties('AWS::ECS::TaskDefinition', {
    ContainerDefinitions: [Match.objectLike({
      Name: 'gateway',
      Secrets: Match.arrayWith([
        Match.objectLike({ Name: 'DB_USER' }),
        Match.objectLike({ Name: 'DB_PASSWORD' }),
        Match.objectLike({ Name: 'LITELLM_MASTER_KEY_SUFFIX' }),
      ]),
    })],
  });
});
