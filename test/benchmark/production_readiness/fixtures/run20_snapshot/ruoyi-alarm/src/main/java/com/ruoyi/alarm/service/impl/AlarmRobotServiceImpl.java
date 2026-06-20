package com.ruoyi.alarm.service.impl;

import java.util.List;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import com.ruoyi.alarm.domain.AlarmRobot;
import com.ruoyi.alarm.mapper.AlarmRobotMapper;
import com.ruoyi.alarm.service.IAlarmRobotService;

/**
 * 机器人管理 Service 实现
 *
 * @author ruoyi
 */
@Service
public class AlarmRobotServiceImpl implements IAlarmRobotService
{
    @Autowired
    private AlarmRobotMapper alarmRobotMapper;

    /**
     * 查询机器人列表
     *
     * @param robot 机器人信息
     * @return 机器人集合
     */
    @Override
    public List<AlarmRobot> selectAlarmRobotList(AlarmRobot robot)
    {
        return alarmRobotMapper.selectAlarmRobotList(robot);
    }

    /**
     * 新增机器人
     *
     * @param robot 机器人信息
     * @return 结果
     */
    @Override
    public int insertAlarmRobot(AlarmRobot robot)
    {
        if (robot.getRobotName() == null || robot.getRobotName().isEmpty())
        {
            throw new IllegalArgumentException("机器人名称不能为空");
        }
        if (robot.getWebhookUrl() == null || robot.getWebhookUrl().isEmpty())
        {
            throw new IllegalArgumentException("Webhook地址不能为空");
        }
        return alarmRobotMapper.insertAlarmRobot(robot);
    }

    /**
     * 修改机器人
     *
     * @param robot 机器人信息
     * @return 结果
     */
    @Override
    public int updateAlarmRobot(AlarmRobot robot)
    {
        if (robot.getRobotId() == null)
        {
            throw new IllegalArgumentException("机器人ID不能为空");
        }
        if (robot.getRobotName() == null || robot.getRobotName().isEmpty())
        {
            throw new IllegalArgumentException("机器人名称不能为空");
        }
        if (robot.getWebhookUrl() == null || robot.getWebhookUrl().isEmpty())
        {
            throw new IllegalArgumentException("Webhook地址不能为空");
        }
        return alarmRobotMapper.updateAlarmRobot(robot);
    }

    /**
     * 删除机器人
     *
     * @param robotId 机器人ID
     * @return 结果
     */
    @Override
    public int deleteAlarmRobotById(Long robotId)
    {
        return alarmRobotMapper.deleteAlarmRobotById(robotId);
    }

    /**
     * 批量删除机器人
     *
     * @param ids 需要删除的ID（逗号分隔）
     * @return 结果
     */
    @Override
    public int deleteAlarmRobotByIds(String ids)
    {
        String[] idArray = ids.split(",");
        int count = 0;
        for (String id : idArray)
        {
            count += deleteAlarmRobotById(Long.parseLong(id));
        }
        return count;
    }

    /**
     * 根据ID查询机器人
     *
     * @param robotId 机器人ID
     * @return 机器人信息
     */
    @Override
    public AlarmRobot selectAlarmRobotById(Long robotId)
    {
        return alarmRobotMapper.selectAlarmRobotById(robotId);
    }
}
